"""去废片配方：口播 / 录课 / 游戏实况 → 方案（Plan）。只做判断，不渲染。

每一刀都写清理由（reason）、把握度（confidence）和一句人话（detail）：
- 停顿：每个停顿按 pace 留一段（tight 0.15s / natural 0.4s / relaxed 0.8s，前后各一半），
  比"保留长度 + SLACK"长的停顿剪掉中间（silence）；更短的原样保留（pause）。删光停顿让人局促（A1）。
- 重说（需要文字稿）：按标点和停顿切成小句；出现"不对 / 我重新说一遍 / sorry, let me say that again"这类重来标记，
  就把标记和它前面被放弃的那遍一起剪掉，保留最后一遍；没有标记时，只有后一句开头和前一句高度相似、
  而且后一句说得更多（同长度的排比句不算）才剪（retake）。重说的 take 才是时间黑洞（E1）。
- 口头禅：只剪单独出现（前后有停顿或标点）的嗯 / 呃 / um / uh（filler）；"那个 / 就是 / like" 只标记不剪。
- 游戏实况（多音轨，或 keep_action=True）：人声的长停顿里，游戏声 / 混音轨有响度爆发或画面频繁切换就保留
  （action），否则剪掉（idle）。没人说话不等于没内容（A3，traces S58）。

剪口规则（硬约束，A2：剪口落在半个字上比不剪更糟）：剪口只落在语音段之间的空隙里（吸附到能量最低点，
离语音边缘至少 GUARD），或者两个相邻词之间能量最低的帧上；绝不落在一个词的中间。

segments 覆盖整个素材、首尾相接，剪掉的片段留在列表里，审阅页可以一键改回保留。
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np

from autocut import transcribe
from autocut.analysis import SR, Media
from autocut.audio import frame_db, resolve_track
from autocut.plan import Captions, Output, Plan, Segment, new_segment_id
from autocut.recipes._common import RecipeError, make_source, mmss, plan_path_for, runs, unique_id

log = logging.getLogger(__name__)

# ---- 停顿
PACES = {"tight": 0.15, "natural": 0.4, "relaxed": 0.8}  # 每个停顿保留多长（秒），前后各一半
SLACK = 0.1  # 停顿比保留长度长出这么多才动刀
PAUSE_SEG_MIN = 0.25  # 两段话之间 ≥ 这么长、又不剪的停顿单独成一个"自然停顿"片段；更短的并进说话片段
SPLIT_SILENCE_MIN = 1.0  # 紧挨着被剪内容的停顿：中间部分 ≥ 此值才单独成"长停顿"片段（改回保留重说时，停顿仍按 pace）
GUARD = 0.03  # 停顿里的剪口离语音段边缘至少这么远
HOP = 0.01  # 能量曲线帧移
SNAP_WINDOW = 0.05  # 停顿里的剪口在目标位置 ±此范围内找能量最低点
WORD_SNAP_BEFORE, WORD_SNAP_AFTER = 0.1, 0.15  # 词间（没有停顿）剪口的搜索窗；t_dtw 偏早，往后多找一点
SNAP_TIE_DB = 1.0  # 比最低点高不到这么多的帧都算"最低"，取离目标最近的

# ---- 句子切分与重说
CLAUSE_GAP = 0.25  # 词间停顿 ≥ 此值算小句边界
SENTENCE_GAP = 0.6  # 词间停顿 ≥ 此值算句子边界
RETAKE_SIM = 0.6  # 没有重来标记时，前一句 vs 后一句同长度前缀的字符相似度阈值
RETAKE_MARKER_SIM = 0.5  # 有重来标记时，用相似度确定被放弃的范围
RETAKE_LONGER = 1.15  # 没有标记时，后一句至少比前一句长这么多倍（+1 字）：排比句长度相近，重说会说得更完整
RETAKE_MIN_CHARS = 4  # 没有标记时，前一句至少这么多字（规范化后）才考虑
RETAKE_MAX_GAP = 10.0  # 两遍之间隔太久就不算重说
RETAKE_LOOKBACK = 2  # 有标记时，往前最多看几句找被放弃的那遍
_SENTENCE_END = set("。.？?！!…")
_STRONG_MARKERS = re.compile(
    r"重新说|重说|再说一遍|再说一次|重来|说错了|重新来|"
    r"let me say that again|let me start over|let me rephrase|let me try that again|start over|scratch that|"
    r"take two|one more time",
    re.I,
)
_STRONG_MAX_EXTRA = 8  # 强标记所在小句最多比标记本身多这么多字，否则是正文里碰巧出现
_WEAK_MARKERS = ("不好意思", "对不起", "等一下", "不对", "不是", "抱歉", "错了", "sorry", "i mean", "oops", "wait", "no no")

# ---- 口头禅
_FILLER_CHARS = set("嗯呃额唔")
_FILLER_EN = re.compile(r"^(u+m+|u+h+|e+r+m*|h+m+|m{2,}|uhm+)$")
_SOFT_FILLERS = re.compile(r"^(啊+|a+h+)$")  # 也可能是有意义的感叹，剪但标拿不准
_AMBIGUOUS = {"那个", "这个", "就是", "就是说", "然后呢", "like", "youknow", "basically", "kindof", "sortof"}

# ---- 游戏实况
ACT_HOP, ACT_WIN = 0.05, 0.2  # 活动检测的响度曲线
BURST_DB = 6.0  # 比该轨（人声停顿里的）中位数响这么多算爆发
BURST_MERGE = 1.0  # 爆发之间隔不到这么久算同一段
BURST_MIN = 0.15  # 爆发总时长至少这么长
EDGE_IGNORE = 0.25  # 离人声这么近的帧不看（混音轨里有人声的尾巴）
ACTION_PAD = 1.0  # 看点前后各留这么久（铺垫与余韵）
IDLE_MIN = 1.0  # 看点之间 / 看点与说话之间的空闲短于此就不剪
VIDEO_CLUSTER = 3.0  # 这么短的时间里有 ≥2 次画面切换，算画面上有动静
_GAME_TITLE = re.compile(r"game|desktop|system|app|游戏|桌面|系统", re.I)


# ------------------------------------------------------------------ 公开入口


def build(
    sources: Sequence[str | Path],
    *,
    out: str | Path | None = None,
    pace: str | float = "natural",
    language: str | None = None,
    track: int | str | None = None,
    retakes: bool = True,
    fillers: bool = True,
    protect: Iterable[Sequence[float]] = (),
    keep_action: bool | str = "auto",
) -> Plan:
    """口播 / 录课 / 游戏实况去废片 → 方案。不保存；plan.path 默认是 <素材同目录>/<stem>.autocut.json。

    pace：tight / natural / relaxed，或直接给每个停顿保留的秒数。
    language：None 自动识别，或 "zh"、"en"。track：分析用的音轨（序号或标题），默认按标题猜人声轨。
    protect：[(start, end), ...] 素材时间，区间内任何判断都不剪，片段标 locked（多个素材时对每个素材都生效）。
    keep_action："auto"（多音轨时开启）/ True / False：人声停顿里有游戏动静就保留。
    """
    t0 = time.perf_counter()
    paths = [Path(p) for p in sources]
    if not paths:
        raise RecipeError("cut 配方至少需要一个素材文件")
    keep = _pace_seconds(pace)
    prot = _protect_ranges(protect)
    if keep_action not in ("auto", True, False):
        raise RecipeError(f"keep_action={keep_action!r} 看不懂：用 'auto'（多音轨时开启）、True 或 False")
    plan_path = plan_path_for(paths[0], Path(out) if out else None)
    output = Output("main", captions=Captions(enabled=True, burn=False), title="去废片")
    plan = Plan(
        recipe="cut",
        sources={},
        outputs=[output],
        options={
            "pace": pace, "keep_pause": keep, "language": language, "track": track, "retakes": retakes,
            "fillers": fillers, "protect": [list(r) for r in prot], "keep_action": keep_action,
        },
        path=plan_path,
    )
    log.info("cut：%d 个素材，pace=%s（每个停顿留 %.2fs），retakes=%s，fillers=%s，keep_action=%s，保护区间 %s",
             len(paths), pace, keep, retakes, fillers, keep_action, prot or "无")
    for p in paths:
        media = Media(p)
        sid = unique_id(p.stem, plan.sources)
        src = make_source(sid, media, plan_path)
        cutter = _SourceCut(media, sid, keep=keep, language=language, track=track, retakes=retakes,
                            fillers=fillers, protect=prot, keep_action=keep_action)
        output.segments.extend(cutter.run())
        src.analysis_track = cutter.atrack
        src.audio_track = cutter.out_track
        src.words = str(cutter.words_path) if cutter.words_path else None
        src.warnings.extend(cutter.warnings)
        plan.sources[sid] = src
        plan.notes.extend(cutter.notes)
    for n, s in enumerate(output.segments, 1):
        s.id = new_segment_id("d", n)
    plan.validate()
    summ = plan.summary()["outputs"][0]
    log.info("cut 完成：%d 个片段，剪掉 %s，时长 %.1fs → %.1fs，拿不准 %d 处，耗时 %.2fs",
             summ["segments"], summ["cut"], summ.get("source_span", output.duration), output.duration,
             len(summ["uncertain"]), time.perf_counter() - t0)
    return plan


def _pace_seconds(pace: str | float) -> float:
    if isinstance(pace, (int, float)) and not isinstance(pace, bool):
        if not 0 <= pace <= 10:
            raise RecipeError(f"pace={pace} 秒超出范围：每个停顿保留的秒数应在 0–10 之间")
        return float(pace)
    if isinstance(pace, str) and pace.strip().lower() in PACES:
        return PACES[pace.strip().lower()]
    raise RecipeError(f"pace={pace!r} 看不懂：用 tight（每个停顿留 0.15s）、natural（0.4s）、relaxed（0.8s），或直接给秒数")


def _protect_ranges(protect: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    out = []
    for r in protect:
        try:
            a, b = (float(x) for x in r)
        except (TypeError, ValueError):
            raise RecipeError(f"protect 里的 {r!r} 看不懂：每一项写成 (开始秒, 结束秒)") from None
        if not 0 <= a < b:
            raise RecipeError(f"protect 区间 ({a}, {b}) 不合法：要求 0 ≤ 开始 < 结束")
        out.append((a, b))
    return sorted(out)


# ------------------------------------------------------------------ 文字处理


def _norm(s: str) -> str:
    """相似度比较用：小写，去掉标点和空白。"""
    return re.sub(r"[\W_]+", "", s.lower())


def _sim(a: str, b: str) -> float:
    """a 与 b 同长度前缀的字符相似度。"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b[: len(a)], autojunk=False).ratio()


def _is_punct(s: str) -> bool:
    return transcribe._is_punct(s)


def _join(texts: Iterable[str]) -> str:
    """拼文字稿：英文词之间加空格，中文和标点直接接上。"""
    out = ""
    for t in texts:
        if out and t and not _is_punct(t) and _spaced(t[0]) and (_spaced(out[-1]) or out[-1] in _ASCII_STOPS):
            out += " "
        out += t
    return out


_ASCII_STOPS = set(",.?!;:)\"")


def _spaced(c: str) -> bool:
    return c.isalnum() and not transcribe._is_cjk(c)


def _marker(text: str) -> tuple[str, str] | None:
    """小句是不是重来标记 → ("strong" | "weak", 标记原文)；不是返回 None。"""
    low = re.sub(r"[^\w\s']+", " ", text.lower()).strip()
    low = re.sub(r"\s+", " ", low)
    m = _STRONG_MARKERS.search(low)
    if m and len(_norm(low)) <= len(_norm(m.group())) + _STRONG_MAX_EXTRA:
        return "strong", m.group()
    rest = low
    for w in _WEAK_MARKERS:
        rest = rest.replace(w, " ")
    if low and not rest.strip():
        return "weak", low
    return None


def _filler_kind(text: str) -> str | None:
    """一个小句（规范化后）是不是口头禅："core" 剪 / "soft" 剪但拿不准 / "ambiguous" 只标记。"""
    t = _norm(text)
    if not t:
        return None
    if all(c in _FILLER_CHARS for c in t) or _FILLER_EN.match(t):
        return "core"
    if _SOFT_FILLERS.match(t):
        return "soft"
    if t in _AMBIGUOUS:
        return "ambiguous"
    return None


# ------------------------------------------------------------------ 数据结构


@dataclass
class _Clause:
    i0: int  # 在非标点词列表里的下标（含）
    i1: int
    sent: int  # 句子序号
    text: str  # 带标点的原文
    start: float
    end: float

    @property
    def norm(self) -> str:
        return _norm(self.text)


@dataclass
class _Decision:
    """对一串词的判断（重说 / 口头禅）。i0..i1 是非标点词下标（含）。"""

    i0: int
    i1: int
    action: str
    reason: str
    confidence: float
    detail: str
    text: str
    evidence: dict[str, Any] = field(default_factory=dict)
    locked: bool = False


@dataclass
class _Unit:
    """一段连续的人声（语音段，或在词间被切开的一部分）。"""

    start: float
    end: float
    words: list[int] = field(default_factory=list)  # 非标点词下标
    decision: _Decision | None = None

    @property
    def kept(self) -> bool:
        return self.decision is None or self.decision.action == "keep"

    @property
    def group(self) -> Any:
        return "speech" if self.decision is None else ("d", id(self.decision))


@dataclass
class _Piece:
    start: float
    end: float
    action: str
    reason: str
    confidence: float
    detail: str
    group: Any  # 相邻且 group 相同的 piece 合并成一个片段
    evidence: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    locked: bool = False


@dataclass
class _Activity:
    """游戏实况：人声停顿里另一条音轨的响度爆发 + 画面切换。"""

    label: str
    base_db: float
    curve: np.ndarray  # ACT_HOP 栅格上的 dB
    bursts: list[tuple[float, float, float]]  # (起, 止, 比中位数高多少 dB)
    video_cuts: list[float]
    video_clusters: list[tuple[float, float, int]]  # (起, 止, 切换次数)

    def max_rise(self, a: float, b: float) -> float:
        i0, i1 = int(a / ACT_HOP), int(math.ceil(b / ACT_HOP))
        seg = self.curve[max(0, i0): max(0, i1)]
        return float(seg.max() - self.base_db) if seg.size else 0.0

    def active_in(self, a: float, b: float) -> list[tuple[float, float, dict[str, Any]]]:
        """[a, b] 里有动静的区间（前后各留 ACTION_PAD，合并后裁到 [a, b]），附证据。"""
        raw: list[tuple[float, float, str, Any]] = []
        for s, e, rise in self.bursts:
            if e > a - ACTION_PAD and s < b + ACTION_PAD:
                raw.append((s, e, "audio", (round(s, 2), round(e, 2), round(rise, 1))))
        for s, e, n in self.video_clusters:
            if e > a - ACTION_PAD and s < b + ACTION_PAD:
                raw.append((s, e, "video", n))
        raw.sort()
        merged: list[list[Any]] = []
        for s, e, kind, ev in raw:
            s, e = s - ACTION_PAD, e + ACTION_PAD
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
                merged[-1][2].append((kind, ev))
            else:
                merged.append([s, e, [(kind, ev)]])
        out = []
        for s, e, evs in merged:
            s, e = max(s, a), min(e, b)
            if e - s <= 0:
                continue
            audio = [ev for k, ev in evs if k == "audio"]
            cuts = [c for c in self.video_cuts if s <= c <= e]
            out.append((s, e, {"bursts": audio, "video_cuts": len(cuts)}))
        return out


# ------------------------------------------------------------------ 文字判断：重说、口头禅


def _clauses(ws: list[dict[str, Any]]) -> list[_Clause]:
    out, sent = [], 0
    i0 = 0
    for i, w in enumerate(ws):
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        gap = (nxt["start"] - w["end"]) if nxt else math.inf
        clause_end = bool(w["after"]) or gap >= CLAUSE_GAP or nxt is None
        if not clause_end:
            continue
        text = _join(t for j in range(i0, i + 1) for t in (ws[j]["text"], ws[j]["after"]) if t)
        out.append(_Clause(i0, i, sent, text, ws[i0]["start"], w["end"]))
        if any(c in _SENTENCE_END for c in w["after"]) or gap >= SENTENCE_GAP:
            sent += 1
        i0 = i + 1
    return out

def judge(ws: list[dict[str, Any]], *, retakes: bool = True, fillers: bool = True) -> list[_Decision]:
    """逐词文字稿（非标点词，带 "after" = 后面跟的标点）→ 重说 / 口头禅判断，按位置排序。纯文字逻辑，不看音频。"""
    cl = _clauses(ws)
    taken: set[int] = set()
    out: list[_Decision] = []
    if retakes:
        out += _retakes_marked(cl, taken)
        out += _retakes_similar(cl, taken)
    if fillers:
        out += _fillers(ws, cl, taken)
    return sorted(out, key=lambda d: d.i0)


def _sent_start(cl: list[_Clause], k: int) -> int:
    while k > 0 and cl[k - 1].sent == cl[k].sent:
        k -= 1
    return k


def _restart_text(cl: list[_Clause], j: int, display: bool = False) -> str:
    """从小句 j 开始的"重说"文本：到它所在句子结束为止。display=True 返回带标点的原文（给人看）。"""
    same = [c for c in cl[j:] if c.sent == cl[j].sent]
    return _join(c.text for c in same) if display else "".join(c.norm for c in same)


def _retakes_marked(cl: list[_Clause], taken: set[int]) -> list[_Decision]:
    out = []
    ci = 0
    while ci < len(cl):
        mk = _marker(cl[ci].text)
        if mk is None:
            ci += 1
            continue
        cj, kinds = ci, [mk]
        while cj + 1 < len(cl) and (m2 := _marker(cl[cj + 1].text)):
            cj += 1
            kinds.append(m2)
        strong = any(k == "strong" for k, _ in kinds)
        marker_text = " / ".join(t for _, t in kinds)
        if cj + 1 >= len(cl) or ci == 0 or (ci - 1) in taken:
            ci = cj + 1
            continue  # 后面没有重说的内容，或前面没有可放弃的那遍：不当重说处理
        restart = cl[cj + 1]
        r = _restart_text(cl, cj + 1)
        lo = _sent_start(cl, ci - 1)
        for _ in range(RETAKE_LOOKBACK - 1):
            lo = _sent_start(cl, lo - 1) if lo > 0 else lo
        best_k, best_s = None, 0.0
        for k in range(ci - 1, lo - 1, -1):
            if k in taken:
                break
            s = _sim("".join(c.norm for c in cl[k:ci]), r)
            if s > best_s + 1e-9 or (best_k is not None and abs(s - best_s) <= 1e-9):
                best_k, best_s = k, max(s, best_s)
        if best_k is not None and best_s >= RETAKE_MARKER_SIM:
            k0, conf = best_k, 0.95 if best_s >= RETAKE_SIM else 0.85
            detail = (f"重说：被 {mmss(restart.start)} 那句「{_short(_restart_text(cl, cj + 1, True))}」取代"
                      f"（相似度 {best_s:.2f}，有重来标记「{marker_text}」）")
        elif strong:
            k0, conf = _sent_start(cl, ci - 1), 0.7
            detail = (f"重说：出现重来标记「{marker_text}」，剪掉它和前面那句，保留 {mmss(restart.start)} 开始的重说；"
                      f"但前后两遍不太像（相似度 {best_s:.2f}），请确认")
        else:
            ci = cj + 1
            continue  # 弱标记（sorry / 不对）又找不到相似的重说：可能是正常说话，不剪
        text = _join(c.text for c in cl[k0: cj + 1])
        out.append(_Decision(cl[k0].i0, cl[cj].i1, "cut", "retake", conf, detail, text,
                             {"similarity": round(best_s, 3), "marker": marker_text,
                              "replaced_by": round(restart.start, 3)}))
        taken.update(range(k0, cj + 1))
        ci = cj + 1
    return out


def _retakes_similar(cl: list[_Clause], taken: set[int]) -> list[_Decision]:
    out = []
    for ci in range(len(cl) - 1):
        if ci in taken or ci + 1 in taken or cl[ci + 1].start - cl[ci].end > RETAKE_MAX_GAP:
            continue
        r = _restart_text(cl, ci + 1)
        best: tuple[int, float] | None = None
        for k in range(ci, _sent_start(cl, ci) - 1, -1):
            if k in taken:
                break
            a = "".join(c.norm for c in cl[k: ci + 1])
            if len(a) < RETAKE_MIN_CHARS or len(r) < len(a) * RETAKE_LONGER + 1:
                continue
            s = _sim(a, r)
            if s >= RETAKE_SIM and (best is None or s > best[1]):
                best = (k, s)
        if best is None:
            continue
        k0, s = best
        restart = cl[ci + 1]
        conf = round(0.6 + 0.25 * (s - RETAKE_SIM) / (1 - RETAKE_SIM), 2)
        detail = f"重说：紧接着 {mmss(restart.start)} 那句「{_short(_restart_text(cl, ci + 1, True))}」开头几乎一样（相似度 {s:.2f}），保留后一遍"
        out.append(_Decision(cl[k0].i0, cl[ci].i1, "cut", "retake", conf, detail,
                             _join(c.text for c in cl[k0: ci + 1]),
                             {"similarity": round(s, 3), "replaced_by": round(restart.start, 3)}))
        taken.update(range(k0, ci + 1))
    return out


def _fillers(ws: list[dict[str, Any]], cl: list[_Clause], taken: set[int]) -> list[_Decision]:
    out = []
    for ci, c in enumerate(cl):
        if ci in taken:
            continue
        kind = _filler_kind(c.text)
        if kind is None:
            continue
        before = ws[c.i0]["start"] - ws[c.i0 - 1]["end"] if c.i0 > 0 else math.inf
        after = ws[c.i1 + 1]["start"] - ws[c.i1]["end"] if c.i1 + 1 < len(ws) else math.inf
        paused = before >= transcribe.SNAP_MIN_SILENCE and after >= transcribe.SNAP_MIN_SILENCE
        word = _join(ws[i]["text"] for i in range(c.i0, c.i1 + 1))
        ev = {"word": word, "p": round(min(ws[i]["p"] for i in range(c.i0, c.i1 + 1)), 3),
              "pause_before": _rnd(before), "pause_after": _rnd(after)}
        where = "前后都有停顿" if paused else "前后有标点、没有明显停顿，剪口落在词间能量最低处"
        if kind == "core":
            d = _Decision(c.i0, c.i1, "cut", "filler", 0.9 if paused else 0.8, f"口头禅「{word}」，{where}", c.text, ev)
        elif kind == "soft":
            d = _Decision(c.i0, c.i1, "cut", "filler", 0.6,
                          f"「{word}」单独出现，多半是口头禅，也可能是有意义的感叹：剪掉了，请确认", c.text, ev)
        else:
            d = _Decision(c.i0, c.i1, "keep", "filler", 0.4,
                          f"「{word}」单独出现，可能是口头禅，也可能在指代或承接：没有剪，需要时一键剪掉", c.text, ev)
        out.append(d)
        taken.add(ci)
    return out


# ------------------------------------------------------------------ 每个素材


class _SourceCut:
    def __init__(self, media: Media, sid: str, *, keep: float, language: str | None, track: int | str | None,
                 retakes: bool, fillers: bool, protect: list[tuple[float, float]], keep_action: bool | str) -> None:
        self.media, self.sid, self.keep = media, sid, keep
        self.language, self.retakes_on, self.fillers_on = language, retakes, fillers
        self.protect = protect
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.words_path: Path | None = None
        n_tracks = len(media.info.audio)
        if n_tracks == 0:
            raise RecipeError(f"{media.path} 没有音轨：cut 配方靠人声判断，请换一个带声音的素材")
        self.atrack = resolve_track(media.path, track) if track is not None else media.voice_track()
        self.out_track = media.mix_track()  # 输出用混音轨；没有就用默认轨（None）
        self.game = n_tracks >= 2 if keep_action == "auto" else bool(keep_action)
        self.duration = media.duration
        self.db = np.zeros(0, np.float32)
        self.items: list[dict[str, Any]] = []  # 文字稿（含标点）
        self.ws: list[dict[str, Any]] = []  # 非标点词，带 "after"（后面跟的标点）
        self.activity: _Activity | None = None

    # ---- 主流程
    def run(self) -> list[Segment]:
        t0 = time.perf_counter()
        m = self.media
        log.info("%s：分析用 %s，输出用 %s，游戏实况模式=%s", m.path.name, m.track_label(self.atrack),
                 m.track_label(self.out_track), self.game)
        regions = [(float(s), min(float(e), self.duration)) for s, e in
                   m.speech(self.atrack, min_silence=transcribe.SNAP_MIN_SILENCE).regions if s < self.duration]
        self.db = frame_db(m.audio(self.atrack), SR, HOP, 0.025)
        t1 = time.perf_counter()
        self._load_words()
        t2 = time.perf_counter()
        if not regions:
            self.notes.append(f"{self.sid}：{m.track_label(self.atrack)} 里没有检测到人声，什么都没剪；"
                              "如果人声在别的音轨上，用 track 参数指定")
            return [self._segment(_Piece(0.0, self.duration, "keep", "idle", 0.3,
                                         f"整段没有检测到人声（{m.track_label(self.atrack)}），没有剪", "all"))]
        decisions = self._decide() if self.ws else []
        self._apply_protect_to_decisions(decisions)
        units = self._units(regions, decisions)
        if self.game:
            self.activity = self._detect_activity(regions)
        t3 = time.perf_counter()
        pieces = self._pieces(units)
        pieces = self._apply_protect_to_pieces(pieces)
        segs = [self._segment(p) for p in self._merge(pieces)]
        log.info("%s：%d 个语音段 → %d 个单元，%d 个文字判断 → %d 个片段；信号 %.2fs，文字稿 %.2fs，判断 %.2fs，组装 %.2fs",
                 m.path.name, len(regions), len(units), len(decisions), len(segs),
                 t1 - t0, t2 - t1, t3 - t2, time.perf_counter() - t3)
        return segs

    def _load_words(self) -> None:
        if not transcribe.available():
            reason = transcribe.unavailable_reason() or "语音识别不可用（whisper-cli 或模型缺失）"
            self.notes.append(f"{self.sid}：{reason}。只剪了停顿；重说和口头禅未处理")
            log.warning("%s：%s", self.media.path.name, reason)
            return
        try:
            items = self.media.words(self.language, self.atrack)
        except Exception as e:  # noqa: BLE001 —— 识别失败要告诉用户，而不是让整个配方失败
            log.exception("语音识别失败：%s", self.media.path)
            self.notes.append(f"{self.sid}：语音识别失败（{e}）。只剪了停顿；重说和口头禅未处理")
            return
        if items is None:
            self.notes.append(f"{self.sid}：语音识别不可用。只剪了停顿；重说和口头禅未处理")
            return
        self.items = items
        self.words_path = self.media.words_cache_path(self.language, self.atrack)
        for it in items:
            if _is_punct(it["text"]):
                if self.ws:
                    self.ws[-1]["after"] += it["text"]
            else:
                self.ws.append({**it, "after": ""})
        log.info("%s：文字稿 %d 词", self.media.path.name, len(self.ws))

    def _decide(self) -> list[_Decision]:
        out = judge(self.ws, retakes=self.retakes_on, fillers=self.fillers_on)
        for d in out:
            log.info("%s：%s %s %.2f：%s", self.media.path.name, d.reason, d.action, d.confidence, d.detail)
        return out

    # ---- 保护区间
    def _overlaps_protect(self, a: float, b: float) -> tuple[float, float] | None:
        for pa, pb in self.protect:
            if pa < b and pb > a:
                return pa, pb
        return None

    def _apply_protect_to_decisions(self, decisions: list[_Decision]) -> None:
        for d in decisions:
            r = self._overlaps_protect(self.ws[d.i0]["start"], self.ws[d.i1]["end"])
            if r is None:
                continue
            d.detail = f"受保护区间 {_ts(r[0])}–{_ts(r[1])}：不剪（原判断：{d.detail}）"
            d.evidence["original"] = {"action": d.action, "reason": d.reason, "confidence": d.confidence}
            d.action, d.reason, d.confidence, d.locked = "keep", "manual", 1.0, True

    def _apply_protect_to_pieces(self, pieces: list[_Piece]) -> list[_Piece]:
        if not self.protect:
            return pieces
        out = []
        for p in pieces:
            if not self._overlaps_protect(p.start, p.end):
                out.append(p)
            elif p.action == "cut":
                out += self._subtract_protect(p)
            else:
                p.locked = True
                out.append(p)
        return out

    def _subtract_protect(self, p: _Piece) -> list[_Piece]:
        """把一刀里落在保护区间的部分改成保留（这些部分都在停顿里，切开不会落在词中间）。"""
        spans: list[tuple[float, float, bool]] = []  # (起, 止, 是否受保护)
        t = p.start
        for pa, pb in self.protect:
            a, b = max(pa, t), min(pb, p.end)
            if b <= a:
                continue
            if a > t:
                spans.append((t, a, False))
            spans.append((a, b, True))
            t = b
        if t < p.end:
            spans.append((t, p.end, False))
        out = []
        for a, b, prot in spans:
            if prot or b - a <= SLACK:  # 剩下的零头太短，不值得一刀
                out.append(_Piece(a, b, "keep", "manual", 1.0, f"受保护区间：保留（原判断：{p.detail}）",
                                  ("prot", id(p)), {"original": {"action": p.action, "reason": p.reason}},
                                  locked=True))
            else:
                out.append(_Piece(a, b, p.action, p.reason, p.confidence, p.detail, p.group, dict(p.evidence)))
        return out

    # ---- 单元：语音段在被剪内容的边界处切开
    def _units(self, regions: list[tuple[float, float]], decisions: list[_Decision]) -> list[_Unit]:
        ws = self.ws
        rstarts = np.array([s for s, _ in regions])
        word_region = [max(0, int(np.searchsorted(rstarts, (w["start"] + w["end"]) / 2, side="right")) - 1)
                       for w in ws]
        splits: dict[int, list[float]] = {}
        for d in decisions:
            for left, right in ((d.i0 - 1, d.i0), (d.i1, d.i1 + 1)):
                if left < 0 or right >= len(ws) or word_region[left] != word_region[right]:
                    continue  # 两个词在不同语音段：剪口自然落在中间的停顿里
                t = self._snap_between_words(ws[left], ws[right])
                splits.setdefault(word_region[left], []).append(t)
        units: list[_Unit] = []
        for ri, (s, e) in enumerate(regions):
            edges = [s, *sorted(t for t in splits.get(ri, []) if s < t < e), e]
            for a, b in zip(edges, edges[1:]):
                if b > a:
                    units.append(_Unit(a, b))
        # 词按中点归到单元
        starts = [u.start for u in units]
        for i, w in enumerate(ws):
            mid = (w["start"] + w["end"]) / 2
            ui = max(0, int(np.searchsorted(starts, mid, side="right")) - 1)
            units[ui].words.append(i)
        owner = {}
        for d in decisions:
            for i in range(d.i0, d.i1 + 1):
                owner[i] = d
        for u in units:
            ds = {id(owner[i]): owner[i] for i in u.words if i in owner}
            if len(ds) == 1 and all(i in owner for i in u.words):
                u.decision = next(iter(ds.values()))
            elif ds:
                log.debug("单元 %.2f–%.2f 里混有被判断和未判断的词，按保留处理", u.start, u.end)
        # 没有词的单元（笑声、没识别出来的半句）夹在同一个判断中间时，跟着剪
        for k in range(1, len(units) - 1):
            u = units[k]
            if u.words or u.decision:
                continue
            prev = next((x.decision for x in reversed(units[:k]) if x.words), None)
            nxt = next((x.decision for x in units[k + 1:] if x.words), None)
            if prev is not None and prev is nxt:
                u.decision = prev
        return units

    # ---- 剪口吸附
    def _snap(self, t: float, lo: float, hi: float, before: float, after: float) -> float:
        """在 [lo, hi] ∩ [t-before, t+after] 里找能量最低的帧；差不多低的取离 t 最近的。"""
        if hi <= lo:
            return (lo + hi) / 2
        a, b = max(lo, t - before), min(hi, t + after)
        if b < a:
            return min(max(t, lo), hi)
        i0, i1 = int(math.ceil(a / HOP - 1e-9)), min(int(math.floor(b / HOP + 1e-9)), len(self.db) - 1)
        if i1 < i0:
            return min(max(t, lo), hi)
        seg = self.db[i0: i1 + 1]
        cand = np.flatnonzero(seg <= seg.min() + SNAP_TIE_DB)
        best = cand[np.argmin(np.abs((i0 + cand) * HOP - t))]
        return float((i0 + best) * HOP)

    def _snap_in_gap(self, t: float, a: float, b: float) -> float:
        guard = min(GUARD, (b - a) / 4)
        w = min(SNAP_WINDOW, max(0.02, self.keep / 4))  # 紧凑节奏下吸附不能把保留的停顿吃掉太多
        return self._snap(t, a + guard, b - guard, w, w)

    def _snap_between_words(self, w0: dict[str, Any], w1: dict[str, Any]) -> float:
        lo = (w0["start"] + w0["end"]) / 2 + HOP
        hi = (w1["start"] + w1["end"]) / 2
        return self._snap(w0["end"], lo, hi, WORD_SNAP_BEFORE, WORD_SNAP_AFTER)

    # ---- 组装片段
    def _unit_piece(self, u: _Unit) -> _Piece:
        d = u.decision
        if d is None:
            if u.words or not self.ws:
                return _Piece(u.start, u.end, "keep", "speech", 0.95 if u.words else 0.9, "说话", u.group)
            return _Piece(u.start, u.end, "keep", "speech", 0.8,
                          "有人声能量但没识别出文字（笑声、咳嗽或背景声），保留", u.group)
        return _Piece(u.start, u.end, d.action, d.reason, d.confidence, d.detail, u.group, dict(d.evidence),
                      d.text, d.locked)

    def _pieces(self, units: list[_Unit]) -> list[_Piece]:
        pieces: list[_Piece] = []
        bounds: list[tuple[_Unit | None, _Unit | None, float, float]] = []
        prev: _Unit | None = None
        for u in units:
            bounds.append((prev, u, prev.end if prev else 0.0, u.start))
            prev = u
        bounds.append((prev, None, prev.end if prev else 0.0, self.duration))
        for gi, (L, R, a, b) in enumerate(bounds):
            if b > a:
                pieces += self._gap_pieces(gi, L, R, a, b)
            if R is not None:
                pieces.append(self._unit_piece(R))
        return [p for p in pieces if p.end - p.start > 1e-6]

    def _gap_pieces(self, gi: int, L: _Unit | None, R: _Unit | None, a: float, b: float) -> list[_Piece]:
        """停顿 [a, b] 两侧是单元 L、R（None = 素材开头/结尾）。按两侧剪留决定停顿怎么分。"""
        n = b - a
        half = self.keep / 2
        hL = (min(half, n / 2) if R is not None else min(half, n)) if L is not None else 0.0
        hR = (min(half, n / 2) if L is not None else min(half, n)) if R is not None else 0.0
        lk = L is not None and L.kept
        rk = R is not None and R.kept
        mid = n - hL - hR  # 用名义位置做决定，吸附只微调剪口
        m0 = a + hL if L is None else self._snap_in_gap(a + hL, a, b)
        m1 = b - hR if R is None else self._snap_in_gap(b - hR, a, b)
        if m1 - m0 < 0.5 * mid:  # 吸附把中间挤没了：退回名义位置
            m0, m1 = a + hL, b - hR
        gL = L.group if L is not None else None
        gR = R.group if R is not None else None
        edge = L is None or R is None
        if (lk or rk) and mid > SLACK and ((lk and rk) or edge or mid >= SPLIT_SILENCE_MIN):
            out = []
            if L is not None:
                out.append(self._glue(L, a, m0))
            out += self._middle(gi, a, b, m0, m1, edge="start" if L is None else "end" if R is None else None)
            if R is not None:
                out.append(self._glue(R, m1, b))
            return out
        if lk and rk:
            if n >= PAUSE_SEG_MIN:
                return [_Piece(a, b, "keep", "pause", 0.9,
                               f"停顿 {_sec(n)}，不超过 {_sec(self.keep + SLACK)}，保留", ("pause", gi), {"gap": _rnd(n)})]
            t = self._snap_in_gap((a + b) / 2, a, b) if gL != gR else b
            return [self._glue(L, a, t), self._glue(R, t, b)]
        if L is None or R is None:  # 素材开头/结尾的短静音：跟着唯一的邻居
            return [self._glue(L or R, a, b)]
        if lk:  # 左留右剪：左边留 hL，其余并进右边那一刀
            return [self._glue(L, a, m0), self._glue(R, m0, b)]
        if rk:  # 左剪右留
            return [self._glue(L, a, m1), self._glue(R, m1, b)]
        if gL == gR:
            return [self._glue(L, a, b)]
        t = self._snap_in_gap((a + b) / 2, a, b)
        return [self._glue(L, a, t), self._glue(R, t, b)]

    def _glue(self, u: _Unit, a: float, b: float) -> _Piece:
        p = self._unit_piece(u)
        p.start, p.end, p.text = a, b, ""
        return p

    def _middle(self, gi: int, a: float, b: float, m0: float, m1: float, edge: str | None) -> list[_Piece]:
        """停顿中间要剪的部分 [m0, m1]：普通素材 → silence；游戏实况 → 有动静的保留（action），其余 idle。"""
        n, kept = b - a, (m0 - a) + (b - m1)
        ev = {"gap": _rnd(n), "kept": _rnd(kept)}
        what = {"start": "开头静音", "end": "结尾静音"}.get(edge or "", "停顿")
        if self.activity is None:
            conf = 0.95 if n >= 1.0 else 0.85
            return [_Piece(m0, m1, "cut", "silence", conf, f"{what} {_sec(n)} → 保留 {_sec(kept)}", ("gap", gi), ev)]
        act = self.activity
        spans = act.active_in(m0, m1)
        # 看点之间、看点与两端之间不到 IDLE_MIN 的空闲并进看点
        chunks: list[list[Any]] = []
        t = m0
        for s, e, aev in spans:
            if chunks and s - chunks[-1][1] < IDLE_MIN:
                chunks[-1][1] = e
                chunks[-1][2].append(aev)
                t = e
                continue
            if s - t < IDLE_MIN:
                s = t
            chunks.append([s, e, [aev]])
            t = e
        if chunks and m1 - chunks[-1][1] < IDLE_MIN:
            chunks[-1][1] = m1
        out: list[_Piece] = []
        t = m0
        for k, (s, e, aevs) in enumerate(chunks):
            if s > t:
                out.append(self._idle(gi, k, t, s, n, kept, what))
            bursts = [x for aev in aevs for x in aev["bursts"]]
            vcuts = sum(aev["video_cuts"] for aev in aevs)
            parts = []
            if bursts:
                lo, hi = min(x[0] for x in bursts), max(x[1] for x in bursts)
                parts.append(f"{act.label}在 {_ts(lo)}–{_ts(hi)} 有 {len(bursts)} 次响度爆发"
                             f"（最多比中位数高 {max(x[2] for x in bursts):.0f} dB）")
            if vcuts:
                parts.append(f"画面切换 {vcuts} 次")
            conf = 0.75 if bursts and vcuts else 0.7 if bursts else 0.6
            out.append(_Piece(s, e, "keep", "action", conf, f"人声停顿 {_sec(n)}，但" + "，".join(parts) + " → 保留",
                              ("gap", gi, "act", k), {**ev, "bursts": bursts, "video_cuts": vcuts}))
            t = e
        if t < m1:
            out.append(self._idle(gi, len(chunks), t, m1, n, kept, what))
        return out

    def _idle(self, gi: int, k: int, s: float, e: float, n: float, kept: float, what: str) -> _Piece:
        act = self.activity
        assert act is not None
        rise = act.max_rise(s, e)
        vc = sum(1 for c in act.video_cuts if s <= c <= e)
        video = f"画面切换 {vc} 次（零星切换不算看点）" if vc else "画面没有切换"
        detail = f"{what} {_sec(n)} 没人说话，{act.label}平稳（最多比中位数高 {rise:.0f} dB），{video} → 剪掉 {_sec(e - s)}"
        conf = 0.8 if vc == 0 else 0.7
        return _Piece(s, e, "cut", "idle", conf, detail, ("gap", gi, "idle", k),
                      {"gap": _rnd(n), "kept": _rnd(kept), "max_rise_db": round(rise, 1), "video_cuts": vc})

    def _merge(self, pieces: list[_Piece]) -> list[_Piece]:
        out: list[_Piece] = []
        for p in pieces:
            if out and out[-1].group == p.group and abs(out[-1].end - p.start) < 1e-6:
                q = out[-1]
                q.end = p.end
                q.locked = q.locked or p.locked
                continue
            out.append(p)
        for p in out:
            if p.reason in ("speech", "manual") or (p.reason == "filler" and p.action == "keep"):
                p.text = self._text_between(p.start, p.end) or p.text
            elif p.reason in ("retake", "filler"):
                p.text = p.text or self._text_between(p.start, p.end)
            if p.reason == "speech" and p.detail == "说话":
                p.detail = f"说话 {_sec(p.end - p.start)}"
        return out

    def _text_between(self, a: float, b: float) -> str:
        parts, inside = [], False
        for it in self.items:
            if _is_punct(it["text"]):
                if inside:
                    parts.append(it["text"])
                continue
            inside = a <= (it["start"] + it["end"]) / 2 < b
            if inside:
                parts.append(it["text"])
        return _join(parts)

    def _segment(self, p: _Piece) -> Segment:
        return Segment(id="", source=self.sid, start=round(p.start, 3), end=round(p.end, 3), action=p.action,
                       reason=p.reason, confidence=round(p.confidence, 2), detail=p.detail, text=p.text,
                       locked=p.locked, evidence=p.evidence)

    # ---- 游戏实况：另一条音轨的响度爆发 + 画面切换
    def _activity_track(self) -> int | None:
        tracks = self.media.info.audio
        others = [t for t in tracks if t.track_number != (self.atrack or 0)]
        for t in others:
            if t.title and _GAME_TITLE.search(t.title):
                return t.track_number
        mix = self.media.mix_track()
        if mix is not None and mix != self.atrack:
            return mix
        return others[0].track_number if others else self.atrack

    def _detect_activity(self, regions: list[tuple[float, float]]) -> _Activity:
        t0 = time.perf_counter()
        m = self.media
        track = self._activity_track()
        label = m.track_label(track) if track != self.atrack else "同一音轨"
        db = frame_db(m.audio(track), SR, ACT_HOP, ACT_WIN)
        times = np.arange(len(db)) * ACT_HOP
        near_voice = np.zeros(len(db), dtype=bool)
        for s, e in regions:
            near_voice[(times >= s - EDGE_IGNORE) & (times <= e + EDGE_IGNORE)] = True
        quiet = db[~near_voice]
        base = float(np.median(quiet)) if quiet.size >= 20 else float(np.median(db)) if db.size else -100.0
        hot = (db > base + BURST_DB) & ~near_voice
        bursts = []
        for i0, i1 in runs(hot, merge_gap=int(BURST_MERGE / ACT_HOP)):
            dur = int(hot[i0: i1 + 1].sum()) * ACT_HOP
            if dur < BURST_MIN:
                continue
            bursts.append((i0 * ACT_HOP, (i1 + 1) * ACT_HOP, float(db[i0: i1 + 1].max() - base)))
        cuts = [c for c in (m.cuts() if m.info.video else [])
                if not any(s - EDGE_IGNORE <= c <= e + EDGE_IGNORE for s, e in regions)]
        clusters: list[tuple[float, float, int]] = []
        grp: list[float] = []
        for c in cuts + [math.inf]:
            if grp and c - grp[0] > VIDEO_CLUSTER:
                if len(grp) >= 2:
                    clusters.append((grp[0], grp[-1], len(grp)))
                grp = []
            grp.append(c)
        log.info("%s：游戏动静看 %s（停顿里中位数 %.1f dB，爆发阈值 +%.0f dB）→ %d 段响度爆发 %s；画面切换 %d 次，%d 簇；%.2fs",
                 m.path.name, label, base, BURST_DB, len(bursts),
                 [(round(s, 2), round(e, 2), round(r, 1)) for s, e, r in bursts], len(cuts), len(clusters),
                 time.perf_counter() - t0)
        return _Activity(label, base, db, bursts, cuts, clusters)


def _short(text: str, n: int = 14) -> str:
    return text if len(text) <= n else text[:n] + "…"


def _sec(x: float) -> str:
    """0.4 → "0.4s"，0.15 → "0.15s"，2.6 → "2.6s"。"""
    return (f"{x:.1f}" if x >= 10 else f"{x:.2f}".rstrip("0").rstrip(".")) + "s"


def _ts(t: float) -> str:
    """片段内的时刻：短素材里 mm:ss 太粗，带一位小数。"""
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _rnd(x: float) -> float | None:
    return None if math.isinf(x) else round(x, 3)
