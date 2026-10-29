"""直播 / 长视频 → N 条候选切片（"高光"配方）。

判断依据（每一项都会作为理由写进方案，分数只用来排序）：
- 响度：短时响度相对前后 ±30 秒滚动中位数的抬升（音量峰）；
- 弹幕：密度相对基线的倍数，并用 burstiness 惩罚复读刷屏（抽奖刷"1"），被惩罚的诱饵照样列出来；
- 文字稿里的情绪关键词（我去、卧槽、哈哈……）；
- 画面：镜头切换（画面剧烈变化）。

每个候选 = 一个 Output，两个 keep 片段：lead_in（铺垫，往前留 3–10 秒）+ highlight（主体 + 反应）。
开头吸附到句首或停顿处，结尾吸附到句子 / 笑声结束之后约 0.5 秒；任何一刀都不落在一句话中间（A2）。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from autocut.analysis import SR, Media
from autocut.audio import frame_db
from autocut.chat import ChatMessage, burstiness, chat_density, distinct_ratio, load_chat
from autocut.loudness import short_term_curve
from autocut.plan import Captions, Output, Plan, Reframe, Segment, new_segment_id
from autocut.recipes._common import (
    finalize,
    RecipeError,
    aspect_size,
    make_source,
    mmss,
    noisy_or,
    plan_path_for,
    rolling_median,
    rolling_percentile,
    runs,
    unique_id,
)

log = logging.getLogger(__name__)

# ---- 信号参数
HOP = 0.5  # 所有曲线的统一时间栅格（秒）
LOUD_WINDOW = 1.0  # 短时响度窗长
BASELINE_HALF = int(30 / HOP)  # 滚动中位数基线：前后各 30 秒
LOUD_HOT_DB = 10.5  # 比基线响这么多才算"音量峰"（普通聊天句首的瞬时抬升在 +7–9dB）
LOUD_FULL_DB = 20.0  # 抬升到这么多，响度分拿满
LOUD_FLOOR_DB = 8.0  # 响度分从这里开始计
CHAT_WINDOW = 5.0
CHAT_HOT_RATIO = 3.0  # 弹幕密度是基线的几倍才算爆发
CHAT_MIN_RATE = 1.0  # 且每秒至少这么多条（冷清的直播间 3 条也能"3 倍"）
CHAT_BASE_FLOOR = 0.25  # 基线下限，避免基线≈0 时倍数爆炸
CHAT_BASE_HALF = int(45 / HOP)  # 弹幕基线：前后各 45 秒的低分位（热闹的直播里中位数会被爆发本身抬高）
CHAT_BASE_Q = 30.0
EVENT_GAP = 3.0  # 声音事件内部，间隔不超过这么多秒算同一段
SPAM_BURST = 0.6  # 最多的那句话占比 ≥ 此值，判为复读刷屏
SPAM_MIN_MSGS = 8
SPAM_PENALTY = 0.35  # 刷屏候选的分数乘这个系数
CLUSTER_GAP = 6.0  # 不同信号的热点相距不到这么多秒，视为同一件事（弹幕比事件晚几秒）
CHAT_LAG = 3.0  # 只有弹幕、没有声音变化时，事件大约发生在弹幕爆发前这么多秒
WEAK_FLOOR = 8.0  # 分数低于此的候选不列出（除非要凑够 n 条）

# ---- 剪辑参数
LEAD_DEFAULT = 4.0
LEAD_MIN, LEAD_MAX = 3.0, 10.0
TAIL_PAD = 0.5  # 结尾：句子 / 笑声结束之后留这么久
REACTION = 2.5  # 弹幕还在炸时，主体之后多留的"反应"时间
EDGE_PAD = 0.15  # 切点离语音边缘至少这么远（落在停顿里）

KEYWORDS = [
    "我去", "卧槽", "我靠", "握草", "哈哈", "笑死", "牛逼", "太牛", "牛啊", "天哪", "天呐", "我的天", "不是吧", "离谱", "绝了",
    "太强", "赢了", "666", "wow", "omg", "oh my god", "no way", "let's go", "insane", "holy",
]
_KW_RE = re.compile("|".join(re.escape(k) for k in sorted(KEYWORDS, key=len, reverse=True)), re.I)
_SENT_END = set("。！？!?.…；;")
_PUNCT = "。！？!?.…，,、；;：:\"'“”‘’（）() ~～"
_LAUGH_TAIL = re.compile(r"[，,、 ]*(哈|呵|嘻|嘿|ha|hah|haha)+[!！?？。.~～ ]*$", re.I)

AUTH_NOTE = "发布前请确认已获得主播授权（直播切片涉及主播的肖像和内容，没有授权不要发）。"


# ------------------------------------------------------------------ 数据结构


@dataclass
class Cluster:
    """一件"值得切的事"：同一时间附近的几种信号合在一起。"""

    t0: float  # 所有信号的最早 / 最晚时刻
    t1: float
    a0: float = 0.0  # 事件本身（声音 / 关键词）的起止；只有弹幕时由弹幕起点倒推
    a1: float = 0.0
    peak: float = 0.0
    reaction_end: float = 0.0  # 弹幕反应持续到哪
    gain: float = 0.0  # 响度比基线高多少 dB（0 = 不是响度触发）
    ratio: float = 0.0  # 弹幕密度倍数
    rate: float = 0.0
    n_msgs: int = 0
    burst: float = 0.0
    distinct: float = 1.0
    top_text: str = ""
    chat_words: list[tuple[str, int]] = field(default_factory=list)
    keywords: list[tuple[float, str]] = field(default_factory=list)
    cuts: int = 0
    audio_driven: bool = False
    spam: bool = False
    score: float = 0.0
    raw_score: float = 0.0
    commentary: bool | None = None  # 事件期间有没有人在说话；None = 不知道（没有文字稿也没有人声轨）


@dataclass
class Clip:
    start: float
    end: float
    t0: float  # lead_in → highlight 的分界
    start_how: str
    end_how: str
    hard_cuts: int = 0  # 没找到不切断语音的位置、只好硬切的次数
    trimmed: bool = False  # 事件本身比允许的最长时长还长，只取了峰值附近


# ------------------------------------------------------------------ 切点：保护语音，不切在一句话中间


class Cutter:
    """在"受保护的语音区间"之外找切点。

    spans：升序、互不重叠的语音区间（有文字稿时按句子切开）。严格落在区间内部的时刻都不是合法切点。
    """

    def __init__(self, spans: list[tuple[float, float]], duration: float, db: np.ndarray | None = None, db_hop: float = 0.01):
        self.spans = spans
        self.duration = duration
        self.db = db
        self.db_hop = db_hop

    def inside(self, t: float) -> tuple[float, float] | None:
        for s, e in self.spans:
            if s + 1e-3 < t < e - 1e-3:
                return s, e
            if s > t:
                break
        return None

    def _prev_end(self, t: float) -> float:
        return max([e for _, e in self.spans if e <= t + 1e-3] or [0.0])

    def _next_start(self, t: float) -> float:
        return min([s for s, _ in self.spans if s >= t - 1e-3] or [self.duration])

    def _start_candidates(self, target: float) -> list[tuple[float, bool]]:
        """[(时刻, 是否句首)]"""
        out: list[tuple[float, bool]] = []
        sp = self.inside(target)
        if sp:
            out.append((sp[0] - min(0.25, (sp[0] - self._prev_end(sp[0])) / 2), True))
            out.append((sp[1] + min(EDGE_PAD, (self._next_start(sp[1]) - sp[1]) / 2), False))
        else:
            pe, ns = self._prev_end(target), self._next_start(target)
            t = target
            if t - pe < EDGE_PAD:
                t = min(pe + EDGE_PAD, (pe + ns) / 2)
            out.append((t, False))
        for s, _ in self.spans:  # 附近的句首也是好选择
            if abs(s - target) <= 2.0:
                out.append((s - min(0.25, (s - self._prev_end(s)) / 2), True))
        return out

    def _end_candidates(self, target: float) -> list[tuple[float, bool]]:
        """[(时刻, 是否贴着句尾 + TAIL_PAD)]"""
        out: list[tuple[float, bool]] = []
        sp = self.inside(target)
        if sp:
            out.append((sp[1] + min(TAIL_PAD, (self._next_start(sp[1]) - sp[1]) / 2), True))
            out.append((sp[0] - min(EDGE_PAD, (sp[0] - self._prev_end(sp[0])) / 2), False))
        else:
            pe, ns = self._prev_end(target), self._next_start(target)
            t = target
            if t - pe < EDGE_PAD:
                t = min(pe + EDGE_PAD, (pe + ns) / 2)
            elif ns - t < EDGE_PAD:
                t = max(pe, (pe + ns) / 2)
            out.append((t, False))
        for _, e in self.spans:
            if 0 <= target - e <= 2.0:
                out.append((e + min(TAIL_PAD, (self._next_start(e) - e) / 2), True))
        return out

    def _hard(self, lo: float, hi: float) -> float:
        """没有合法切点时的退路：[lo, hi] 里电平最低的位置。"""
        if self.db is None or hi <= lo:
            return (lo + hi) / 2
        a, b = int(lo / self.db_hop), max(int(hi / self.db_hop), int(lo / self.db_hop) + 1)
        seg = self.db[a:b]
        return lo + float(np.argmin(seg)) * self.db_hop if len(seg) else (lo + hi) / 2

    def snap_start(self, target: float, lo: float, hi: float) -> tuple[float, str]:
        lo, hi = max(0.0, lo), min(self.duration, max(hi, lo))
        target = min(max(target, lo), hi)
        cands = [(t, nice) for t, nice in self._start_candidates(target) if lo - 1e-6 <= t <= hi + 1e-6 and not self.inside(t)]
        if not cands:
            return round(self._hard(lo, hi), 3), "hard"
        t, nice = min(cands, key=lambda c: abs(c[0] - target) - (0.6 if c[1] else 0.0))
        return round(max(t, 0.0), 3), "sentence" if nice else "gap"

    def snap_end(self, target: float, lo: float, hi: float) -> tuple[float, str]:
        lo, hi = max(0.0, lo), min(self.duration, max(hi, lo))
        target = min(max(target, lo), hi)
        cands = [(t, nice) for t, nice in self._end_candidates(target) if lo - 1e-6 <= t <= hi + 1e-6 and not self.inside(t)]
        if not cands:
            if self.duration <= hi + 1e-6 and not self.inside(self.duration):
                return round(self.duration, 3), "gap"
            return round(self._hard(lo, hi), 3), "hard"
        t, nice = min(cands, key=lambda c: abs(c[0] - target) - (0.6 if c[1] else 0.0))
        return round(min(t, self.duration), 3), "sentence" if nice else "gap"


def _sentences(words: list[dict[str, Any]]) -> list[tuple[float, float, str]]:
    """逐词文字稿 → 句子 [(起, 止, 文本)]：遇到句末标点，或与下一个词间隔 > 0.5s 就断句。"""
    out: list[tuple[float, float, str]] = []
    cur: list[dict[str, Any]] = []

    def flush() -> None:
        toks = [w for w in cur if w["end"] > w["start"]]
        if toks:
            text = ""
            for w in cur:
                t = str(w["text"])
                text += (" " + t) if text and t[:1].isascii() and text[-1:].isascii() and t[:1].isalnum() else t
            out.append((float(toks[0]["start"]), float(toks[-1]["end"]), text.strip()))
        cur.clear()

    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if str(w["text"]).strip() and str(w["text"]).strip()[-1] in _SENT_END or nxt is None or nxt["start"] - w["end"] > 0.5:
            flush()
    return out


def protected_spans(regions: list[tuple[float, float]], sents: list[tuple[float, float, str]] | None) -> list[tuple[float, float]]:
    """语音检测的区间 + 句子边界 → 受保护区间。有文字稿时，长段连续说话也能在句与句之间切。

    没有对应文字的部分（音效、笑声）单独保留为受保护区间，避免切在笑声中间。"""
    spans: list[tuple[float, float]] = []
    if not sents:
        spans = [tuple(r) for r in regions]  # type: ignore[misc]
    else:
        for a, b in regions:
            inside = [(max(s, a), min(e, b)) for s, e, _ in sents if e > a and s < b]
            cur = a
            for s, e in inside:
                if s - cur > 0.3:
                    spans.append((cur, s))
                spans.append((s, e))
                cur = max(cur, e)
            if b - cur > 0.3 or not inside:
                spans.append((cur, b))
        spans += [(s, e) for s, e, _ in sents if not any(a < e and s < b for a, b in regions)]
    spans.sort()
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s < merged[-1][1] - 1e-3:  # 真重叠才合并；首尾相接的保留，句与句之间仍可切
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(a, b) for a, b in merged]


# ------------------------------------------------------------------ 信号


def _loudness_grid(x: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """短时响度（LUFS 刻度）重采样到统一栅格。长录像分块算，避免整段 FFT 吃掉几个 GB 内存。"""
    chunk, ov = 300 * SR, 4 * SR
    if len(x) <= chunk + ov:
        t, db = short_term_curve(x, SR, window=LOUD_WINDOW, hop=HOP)
    else:
        ts, ds = [], []
        for a in range(0, len(x), chunk):
            lo, hi = max(0, a - ov), min(len(x), a + chunk + ov)
            t, db = short_term_curve(x[lo:hi], SR, window=LOUD_WINDOW, hop=HOP)
            t = t + lo / SR
            keep = (t >= a / SR - 1e-9) & (t < (a + chunk) / SR - 1e-9)
            ts.append(t[keep])
            ds.append(db[keep])
        t, db = np.concatenate(ts), np.concatenate(ds)
    return np.interp(grid, t, np.maximum(db, -70.0))


def _keyword_hits(words: list[dict[str, Any]] | None) -> list[tuple[float, str]]:
    if not words:
        return []
    text, owner = "", []
    for i, w in enumerate(words):
        t = str(w["text"])
        if text and t[:1].isascii() and t[:1].isalnum() and text[-1:].isascii() and text[-1:].isalnum():
            text += " "
            owner.append(i)
        text += t
        owner += [i] * len(t)
    return [(float(words[owner[m.start()]]["start"]), m.group(0).lower()) for m in _KW_RE.finditer(text)]


def _chat_hot(msgs: list[ChatMessage], grid: np.ndarray, duration: float) -> tuple[np.ndarray, np.ndarray]:
    """弹幕密度（条/秒）与相对基线的倍数，已重采样到统一栅格。"""
    t, rate = chat_density(msgs, duration, hop=HOP, window=CHAT_WINDOW)
    rate = np.interp(grid, t, rate)
    base = np.maximum(rolling_percentile(rate, CHAT_BASE_HALF, CHAT_BASE_Q), CHAT_BASE_FLOOR)
    return rate, rate / base


def _event_span(events: list[tuple[float, float, str]], peak: float) -> tuple[float, float]:
    """声音 / 关键词事件的起止：从含峰值的那一段出发，向两侧并入间隔 ≤ EVENT_GAP 的相邻事件。
    （不能直接取全部事件的并集：簇里可能链着一段远处的、无关的声音。）"""
    ev = sorted(events)
    seed = next((e for e in ev if e[0] - 1e-6 <= peak <= e[1] + 1e-6), min(ev, key=lambda e: abs((e[0] + e[1]) / 2 - peak)))
    a0, a1 = seed[0], seed[1]
    changed = True
    while changed:
        changed = False
        for e in ev:
            if (a0 - EVENT_GAP <= e[1] and e[0] < a0) or (a1 + EVENT_GAP >= e[0] and e[1] > a1):
                if e[0] < a0 or e[1] > a1:
                    a0, a1, changed = min(a0, e[0]), max(a1, e[1]), True
    return float(a0), float(a1)


def _find_clusters(
    grid: np.ndarray,
    gain: np.ndarray,
    rate: np.ndarray | None,
    ratio: np.ndarray | None,
    msgs: list[ChatMessage],
    hits: list[tuple[float, str]],
    cuts: list[float],
) -> list[Cluster]:
    """各信号的热点区间 → 按时间邻近合并成"事件"，再逐个算分。"""
    # (起, 止, 种类)
    hot: list[tuple[float, float, str]] = []
    for a, b in runs(gain >= LOUD_HOT_DB, merge_gap=3):
        hot.append((float(grid[a] - HOP), float(grid[b] + HOP), "loud"))
    if ratio is not None and rate is not None:
        for a, b in runs((ratio >= CHAT_HOT_RATIO) & (rate >= CHAT_MIN_RATE), merge_gap=4):
            hot.append((float(grid[a]), float(grid[b]), "chat"))
    for t, _ in hits:
        hot.append((t - 1.0, t + 2.0, "kw"))
    hot.sort()
    groups: list[list[tuple[float, float, str]]] = []
    for h in hot:
        if groups and h[0] - max(x[1] for x in groups[-1]) <= CLUSTER_GAP:
            groups[-1].append(h)
        else:
            groups.append([h])

    clusters: list[Cluster] = []
    for g in groups:
        c = Cluster(t0=min(x[0] for x in g), t1=max(x[1] for x in g))
        loud = [x for x in g if x[2] == "loud"]
        chat = [x for x in g if x[2] == "chat"]
        kw = [x for x in g if x[2] == "kw"]
        lo_i, hi_i = np.searchsorted(grid, c.t0), np.searchsorted(grid, c.t1, side="right")
        if loud:
            seg = gain[lo_i:hi_i]
            c.gain = float(seg.max())
            c.peak = float(grid[lo_i + int(seg.argmax())])
        if kw:
            c.keywords = [(t, w) for t, w in hits if c.t0 <= t <= c.t1]
        c.audio_driven = bool(loud or kw)
        if c.audio_driven:
            c.a0, c.a1 = _event_span(loud + kw, c.peak if loud else c.keywords[0][0])
            if not loud:
                c.peak = c.keywords[0][0]
        if chat and rate is not None and ratio is not None:
            cs, ce = min(x[0] for x in chat), max(x[1] for x in chat)
            ci, cj = np.searchsorted(grid, cs), np.searchsorted(grid, ce, side="right")
            c.ratio = float(ratio[ci:cj].max())
            c.rate = float(rate[ci:cj].max())
            if not c.audio_driven:
                c.peak = float(grid[ci + int(rate[ci:cj].argmax())])
            in_chat = [m for m in msgs if cs - CHAT_WINDOW / 2 <= m.t <= ce + CHAT_WINDOW / 2]
            c.n_msgs = len(in_chat)
            c.burst = burstiness(in_chat)
            c.distinct = distinct_ratio(in_chat)
            cnt = Counter(" ".join(m.text.split()).casefold() for m in in_chat)
            if cnt:
                c.top_text = cnt.most_common(1)[0][0]
            c.chat_words = [(w, n) for w, n in Counter(
                k.lower() for m in in_chat for k in _KW_RE.findall(m.text)).most_common(3)]
            c.spam = c.burst >= SPAM_BURST and c.n_msgs >= SPAM_MIN_MSGS
            c.reaction_end = ce
            if not c.audio_driven:
                c.a0 = max(0.0, cs + 1.5 - CHAT_LAG)  # 5 秒窗口让热点区间提前了约 1.5s；事件再比弹幕早几秒
                c.a1 = c.a0 + 2 * CHAT_LAG
        c.cuts = sum(1 for t in cuts if c.a0 - 2 <= t <= c.a1 + 2)
        c.t1 = max(c.t1, c.a1)
        s_l = min(max((c.gain - LOUD_FLOOR_DB) / (LOUD_FULL_DB - LOUD_FLOOR_DB), 0.0), 1.0) if c.gain else 0.0
        s_c = min(max(np.log2(max(c.ratio, 1e-9) / 2.0) / 2.0, 0.0), 1.0) if c.ratio else 0.0
        s_k = min(len(c.keywords) / 2.0, 1.0)
        s_v = min(c.cuts / 2.0, 1.0)
        c.raw_score = 100.0 * noisy_or([0.7 * s_l, 0.8 * s_c, 0.5 * s_k, 0.35 * s_v])
        c.score = c.raw_score * (SPAM_PENALTY if c.spam else 1.0)
        clusters.append(c)
    return clusters


# ------------------------------------------------------------------ 把"一件事"变成一个切片


def fit_clip(c: Cluster, cut: Cutter, duration: float, lmin: float, lmax: float) -> Clip:
    """铺垫（3–10s）+ 主体（到句子 / 笑声结束后 0.5s）+ 必要的反应段，凑进 [lmin, lmax]，所有切点避开语音中间。"""
    hard = 0

    # 事件起点：落在句子中间就退到句首（前提是这句不太长），这样 lead_in 与 highlight 的分界在句子边界
    t0 = c.a0
    sp = cut.inside(t0)
    if sp and t0 - sp[0] <= 3.0:
        t0 = sp[0]
    t0 = min(max(t0, 0.0), duration)
    # 事件终点 + 0.5s 的收尾
    a1 = max(c.a1, t0 + 1.0)
    end_main, how_end = cut.snap_end(min(a1 + TAIL_PAD, duration), a1, min(a1 + 4.0, duration))
    end_main = max(end_main, min(a1, duration))

    trimmed = False
    # 事件本身太长：围绕峰值取一段
    if end_main - t0 > lmax - LEAD_MIN:
        trimmed = True
        peak = min(max(c.peak, t0), end_main)
        t0n, _ = cut.snap_start(max(peak - 0.4 * (lmax - LEAD_MIN), t0), t0, peak)
        t0 = t0n
        end_main, how_end = cut.snap_end(min(t0 + (lmax - LEAD_MIN) - 0.5, duration), peak, min(t0 + lmax - LEAD_MIN, duration))

    lead = min(LEAD_DEFAULT, t0)
    start, how_start = cut.snap_start(t0 - lead, t0 - LEAD_MAX, max(t0 - min(LEAD_MIN, t0), 0.0))
    if how_start == "hard":
        hard += 1
    if how_end == "hard":
        hard += 1

    # 凑最短时长：先把弹幕反应段补进来，不够再往后延
    end = end_main
    want_tail = end_main + REACTION if c.reaction_end > end_main + 1.0 else end_main
    target = max(start + lmin, min(want_tail, start + lmax))
    if end < target:
        end, how_end = cut.snap_end(min(target, duration), min(start + lmin, duration), min(start + lmax, duration))
        if how_end == "hard":
            hard += 1
    # 到了素材末尾仍不够长：把起点往前挪
    if end - start < lmin - 1e-6:
        start, how_start = cut.snap_start(end - lmin, max(end - lmax, 0.0), min(end - lmin, t0))
        if how_start == "hard":
            hard += 1
    # 超长：先收反应段，再收铺垫
    if end - start > lmax + 1e-6:
        end, how_end = cut.snap_end(start + lmax, max(end_main, start + lmin), start + lmax)
        if end - start > lmax + 1e-6:
            start, how_start = cut.snap_start(end - lmax, end - lmax, t0 - min(LEAD_MIN, t0))
    return Clip(start=start, end=end, t0=max(t0, start), start_how=how_start, end_how=how_end, hard_cuts=hard, trimmed=trimmed)


# ------------------------------------------------------------------ 文案


def _chat_phrase(c: Cluster) -> str:
    top = c.top_text if len(c.top_text) <= 8 else c.top_text[:8] + "…"
    if c.spam:
        return (f"弹幕 {c.ratio:.1f}× 爆发，但 {c.burst:.0%} 是同一句「{top}」"
                "（疑似抽奖刷屏，已扣分，没选）")
    return f"弹幕 {c.ratio:.1f}× 爆发（峰值 {c.rate:.1f} 条/秒）"


def reasons_for(c: Cluster) -> list[str]:
    out: list[str] = []
    if c.ratio:
        out.append(_chat_phrase(c))
    if c.gain:
        out.append(f"音量峰 +{c.gain:.0f}dB")
    if c.keywords:
        seen: list[str] = []
        for _, w in c.keywords:
            if w not in seen:
                seen.append(w)
        out.append("关键词" + "、".join(f"'{w}'" for w in seen[:3]))
    if c.chat_words and not c.spam:
        w, n = c.chat_words[0]
        if n >= 3:
            out.append(f"弹幕反应「{w}」×{n}")
    if c.gain and c.commentary is False:
        out.append("无解说，但游戏声爆发" + (" + 画面剧烈变化" if c.cuts else ""))
    elif c.cuts:
        out.append(f"画面剧烈变化（{c.cuts} 次镜头切换）")
    if c.ratio and not c.audio_driven and not c.spam:
        out.append("只有弹幕，声音和画面没有明显变化，把握度较低")
    return out


def _title(c: Cluster, t0: float, end: float, sents: list[tuple[float, float, str]] | None) -> str:
    """该段最有代表性的一句话：含关键词的第一句，否则主体里最长的一句；没有文字稿就用时间 + 主因。"""
    if sents:
        inside = [s for s in sents if s[1] > t0 and s[0] < end and s[2].strip(_PUNCT)]
        if inside:
            kw_t = [t for t, _ in c.keywords]
            pick = next((s for s in inside if any(s[0] - 0.2 <= t <= s[1] + 0.2 for t in kw_t)), None)
            pick = pick or max(inside, key=lambda s: len(s[2].strip(_PUNCT)))
            text = _LAUGH_TAIL.sub("", pick[2].strip()).strip(_PUNCT) or pick[2].strip()
            return text if len(text) <= 24 else text[:23] + "…"
    why = "弹幕爆发" if (c.ratio and not c.gain) else "音量峰" if c.gain else "高光"
    return f"{mmss(c.a0)} {why}"


def _text_between(sents: list[tuple[float, float, str]] | None, a: float, b: float) -> str:
    if not sents:
        return ""
    return " ".join(s[2] for s in sents if s[1] > a and s[0] < b)[:200]


def _overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / max(min(a[1] - a[0], b[1] - b[0]), 1e-6)


# ------------------------------------------------------------------ 竖屏跟踪裁切


_TRACK_FPS = 5.0
_TRACK_THUMB = (96, 54)
_TRACK_DIFF = 14  # 帧差超过这个灰度值才算"这里动了"（滤掉压缩噪点）
_TRACK_MIN_MASS = 0.002  # 动的像素不到画面的 0.2%，认为没有可跟的运动，裁切框原地不动


def track_keyframes(path: Path, start: float, end: float, src_ar: float, target_ar: float) -> list[list[float]]:
    """用帧差找画面运动的重心，平滑后得到裁切框中心的关键帧 [[输出时间, 中心x, 中心y], ...]（0–1，输出时间从 0 起，每秒一个）。

    只在需要裁切的那个方向上移动（16:9 → 9:16 只动 x）；裁切框始终留在画面内。运动很少时保持居中。
    失败（解码出错）返回 []，由调用方退回居中裁切。"""
    w, h = _TRACK_THUMB
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(path),
           "-map", "0:v:0", "-an", "-vf", f"fps={_TRACK_FPS},scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    n = len(r.stdout) // (w * h)
    if r.returncode != 0 or n < 3:
        log.warning("跟踪裁切：%s [%.1f, %.1f] 解码失败或帧数不足（%d），退回居中裁切", path.name, start, end, n)
        return []
    fr = np.frombuffer(r.stdout, dtype=np.uint8, count=n * w * h).reshape(n, h, w).astype(np.int16)
    d = np.abs(np.diff(fr, axis=0))
    d = np.where(d > _TRACK_DIFF, d, 0).astype(np.float64)
    mass = d.sum(axis=(1, 2))
    xs, ys = (np.arange(w) + 0.5) / w, (np.arange(h) + 0.5) / h
    cx = np.where(mass > 0, (d.sum(axis=1) @ xs) / np.maximum(mass, 1e-9), 0.5)
    cy = np.where(mass > 0, (d.sum(axis=2) @ ys) / np.maximum(mass, 1e-9), 0.5)
    still = mass < _TRACK_MIN_MASS * w * h * 255 * 0.2
    cx[still], cy[still] = np.nan, np.nan

    def smooth(v: np.ndarray) -> np.ndarray:
        # 无运动的帧沿用前一个有效值，再做 1 秒中位数 + 指数平滑，避免镜头抖动
        out, last = v.copy(), 0.5
        for i in range(len(out)):
            if np.isnan(out[i]):
                out[i] = last
            last = out[i]
        med = rolling_median(out, int(_TRACK_FPS // 2))
        ema, acc = np.empty_like(med), med[0]
        for i, m in enumerate(med):
            acc = acc + 0.3 * (m - acc)
            ema[i] = acc
        return ema

    cx_s, cy_s = smooth(cx), smooth(cy)
    cw, ch = min(1.0, target_ar / src_ar), min(1.0, src_ar / target_ar)  # 裁切框占画面的比例
    kfs: list[list[float]] = []
    for t in np.arange(0.0, end - start + 1e-6, 1.0):
        i = min(int(t * _TRACK_FPS), len(cx_s) - 1)
        x = float(np.clip(cx_s[i], cw / 2, 1 - cw / 2)) if cw < 1 else 0.5
        y = float(np.clip(cy_s[i], ch / 2, 1 - ch / 2)) if ch < 1 else 0.5
        kfs.append([round(float(t), 2), round(x, 3), round(y, 3)])
    return kfs


# ------------------------------------------------------------------ 配方入口


def build(
    sources: list[Path],
    *,
    out: Path | None = None,
    n: int = 3,
    candidates: int | None = None,
    length: tuple[float, float] = (15, 30),
    aspect: str | None = "9:16",
    chat: Path | str | None = None,
    language: str | None = None,
    reframe: str = "center",
) -> Plan:
    """直播 / 长视频 → n 条（外加若干未选中的）候选切片。

    n：要几条；candidates：候选上限（含诱饵和未选中的），默认 n+5；length：每条 (最短, 最长) 秒；
    aspect：成片画幅，默认 9:16 竖屏，None 跟随素材；chat：弹幕 jsonl（每行 {"t","user","text"}）；
    language：交给语音识别的语言（None 自动）；reframe："center" 居中裁切，"track" 用帧差找运动重心、让裁切框跟着走。
    """
    t_start = time.perf_counter()
    if not sources:
        raise RecipeError("highlights 需要一个视频素材：build([Path('stream.mp4')], ...)")
    if n < 1:
        raise RecipeError(f"n={n}：至少要 1 条")
    if reframe not in ("center", "track"):
        raise RecipeError(f"reframe={reframe!r}：应为 'center'（居中裁切）或 'track'（跟着画面运动的重心）")
    lmin, lmax = float(length[0]), float(length[1])
    if not 0 < lmin <= lmax:
        raise RecipeError(f"length={length!r} 不合法：应为 (最短秒, 最长秒)，且 0 < 最短 ≤ 最长，例如 (15, 30)")
    cap = candidates if candidates is not None else n + 5
    cap = max(cap, n)

    media = Media(sources[0])
    if media.info.audio == ():
        raise RecipeError(f"{media.path} 没有音轨，高光判断需要声音（响度 / 语音）；请换一个有声音的素材")
    notes: list[str] = []
    if len(sources) > 1:
        notes.append(f"highlights 一次只处理一个素材，只用了 {Path(sources[0]).name}；其余 {len(sources) - 1} 个请分别运行")
    dur = media.duration
    log.info("highlights：%s（%.1fs），n=%d，候选上限 %d，长度 %s，画幅 %s", media.path.name, dur, n, cap, (lmin, lmax), aspect)

    # ---- 信号
    grid = np.arange(0.0, dur, HOP)
    voice_track = media.voice_track()
    t_sig = time.perf_counter()
    x = media.audio()
    db_loud = _loudness_grid(x, grid)
    gain = db_loud - rolling_median(db_loud, BASELINE_HALF)
    speech = media.speech()
    words = media.words(language=language)
    sents = _sentences(words) if words else None
    if words is None:
        notes.append("没有可用的语音识别（文字稿）：关键词信号没有启用，切点只依据语音活动检测，标题用时间代替；"
                     "装好语音识别后重跑可以得到关键词理由和更准的句子边界")
    cuts = media.cuts()
    log.info("信号：响度 %d 点（最大抬升 %.1fdB）、语音 %d 段、文字稿 %s、镜头切点 %d 个，耗时 %.2fs",
             len(grid), float(gain.max()) if len(gain) else 0.0, len(speech.regions),
             f"{len(words)} 词" if words else "无", len(cuts), time.perf_counter() - t_sig)

    msgs: list[ChatMessage] = []
    rate = ratio = None
    if chat is not None:
        chat_path = Path(chat)
        try:
            msgs = load_chat(chat_path)
        except FileNotFoundError:
            raise RecipeError(f"找不到弹幕文件 {chat_path}；格式是 JSONL，每行 {{\"t\": 秒, \"user\": 名字, \"text\": 内容}}") from None
        except ValueError as e:
            raise RecipeError(f"弹幕文件读不了：{e}") from e
        rate, ratio = _chat_hot(msgs, grid, dur)
        log.info("弹幕：%d 条，峰值密度 %.1f 条/秒，最大倍数 %.1f×", len(msgs), float(rate.max()), float(ratio.max()))
    else:
        notes.append("没有提供弹幕文件（chat=...）：只用音量、关键词和画面变化判断；有弹幕时能多一路信号，也能识别抽奖刷屏")

    # ---- 候选
    hits = _keyword_hits(words)
    clusters = _find_clusters(grid, gain, rate, ratio, msgs, hits, cuts)
    # 事件期间有没有人在解说：有人声轨看人声轨，否则看文字稿；都没有就不下结论
    voice_regions = media.speech(track=voice_track).regions if voice_track is not None else None
    for c in clusters:
        if voice_regions is not None:
            c.commentary = any(s < c.a1 and e > c.a0 for s, e in voice_regions)
        elif sents is not None:
            c.commentary = any(s[1] > c.a0 and s[0] < c.a1 for s in sents)

    # 响度偏弱的补位：不够 n 个像样的候选时，用响度最突出的几处凑数（把握度低，明说）
    real = [c for c in clusters if not c.spam and c.score >= WEAK_FLOOR]
    if len(real) < n:
        from autocut.loudness import top_peaks

        have = [(c.t0 - 5, c.t1 + 5) for c in clusters]
        for t, g in top_peaks(grid, gain, n=(n - len(real)) * 3, min_distance=20.0):
            if g < 3.0 or any(a <= t <= b for a, b in have):
                continue
            c = Cluster(t0=t - 1, t1=t + 1, a0=t - 1, a1=t + 1, peak=t, gain=float(g), audio_driven=True)
            c.cuts = sum(1 for ct in cuts if t - 3 <= ct <= t + 3)
            c.raw_score = c.score = 100.0 * noisy_or([0.7 * min(max((g - LOUD_FLOOR_DB) / (LOUD_FULL_DB - LOUD_FLOOR_DB), 0.0), 1.0), 0.35 * min(c.cuts / 2.0, 1.0)]) or 2.0
            clusters.append(c)
            have.append((t - 5, t + 5))
            if len([k for k in clusters if not k.spam]) >= n:
                break

    spans = protected_spans([tuple(r) for r in speech.regions], sents)
    cutter = Cutter(spans, dur, db=frame_db(x, SR, 0.01, 0.025), db_hop=0.01)
    clusters.sort(key=lambda c: -c.score)
    fitted: list[tuple[Cluster, Clip]] = []
    for c in clusters:
        if c.score < 1.0:
            continue
        clip = fit_clip(c, cutter, dur, lmin, lmax)
        if clip.end - clip.start < 1.0:
            continue
        if any(_overlap_ratio((clip.start, clip.end), (o.start, o.end)) > 0.5 for _, o in fitted):
            log.debug("候选 %.1f–%.1f 与分数更高的候选重叠，合并掉", clip.start, clip.end)
            continue
        fitted.append((c, clip))
    fitted = fitted[:cap]

    chosen = [i for i, (c, _) in enumerate(fitted) if not c.spam][:n]
    if len(chosen) < n:
        notes.append(f"只找到 {len(chosen)} 条像样的高光（要 {n} 条）：整段素材里没有更多明显的音量峰 / 弹幕爆发 / 关键词；"
                     "可以放宽 length，或手动在审阅页补一条")

    # ---- 方案
    size = aspect_size(aspect)
    plan_path = plan_path_for(media.path, out)
    sid = unique_id(media.path.stem, set())
    src = make_source(sid, media, plan_path)
    if words is not None:
        wp = media.words_cache_path(language=language)
        if wp is not None:
            src.words = str(wp)
    v = media.info.video
    needs_reframe = bool(size and v and abs(size[0] / size[1] - v.display_aspect) > 0.02)

    outputs: list[Output] = []
    for rank, (c, clip) in enumerate(fitted, 1):
        selected = (rank - 1) in chosen
        reasons = reasons_for(c)
        if not selected:
            if c.spam:
                notes.append(
                    f"候选 clip_{rank}（{mmss(clip.start)}–{mmss(clip.end)}）弹幕 {c.ratio:.1f}× 爆发，但 {c.burst:.0%} 是同一句"
                    f"「{c.top_text}」（{c.n_msgs} 条里只有 {c.distinct:.0%} 不重复）"
                    + ("，声音和画面都没有明显变化" if not c.audio_driven and not c.cuts else "")
                    + "，判为抽奖刷屏，没选"
                )
            else:
                reasons.append(f"排名第 {rank}，只取前 {n} 条，没选")
        segs: list[Segment] = []
        sid_n = 0
        uncertain_why = []
        if clip.hard_cuts:
            uncertain_why.append("这里一直有人说话，找不到不打断句子的位置，只能在电平最低处硬切")
        if clip.trimmed:
            uncertain_why.append("这件事持续很久，超过了最长时长，只取了峰值附近")
        conf_main = 0.55 + 0.4 * min(c.score / 80.0, 1.0)
        if not c.audio_driven and c.ratio:
            conf_main = min(conf_main, 0.6)  # 只有弹幕：没有声音佐证
        if uncertain_why:
            conf_main = min(conf_main, 0.6)
        ev = {k: v2 for k, v2 in {
            "loud_gain_db": round(c.gain, 1) if c.gain else None,
            "chat_ratio": round(c.ratio, 1) if c.ratio else None,
            "chat_msgs": c.n_msgs or None,
            "burstiness": round(c.burst, 2) if c.ratio else None,
            "keywords": [w for _, w in c.keywords[:5]] or None,
            "cuts": c.cuts or None,
            "peak": round(c.peak, 2),
        }.items() if v2 is not None}
        if clip.t0 - clip.start > 0.05:
            sid_n += 1
            segs.append(Segment(
                id=new_segment_id("c", sid_n), source=sid, start=float(clip.start), end=float(clip.t0), reason="lead_in",
                confidence=0.5 if clip.start_how == "hard" else 0.85,
                detail=f"铺垫 {clip.t0 - clip.start:.1f}s，让观众知道发生了什么；起点{_how(clip.start_how, True)}",
                text=_text_between(sents, clip.start, clip.t0),
            ))
        sid_n += 1
        tail = clip.end - c.a1
        segs.append(Segment(
            id=new_segment_id("c", sid_n), source=sid, start=float(clip.t0), end=float(clip.end), reason="highlight",
            confidence=round(conf_main, 2),
            detail=(f"{reasons[0] if reasons else '高光'}；结尾{_how(clip.end_how, False)}"
                    + (f"（事件结束后留了 {tail:.1f}s）" if tail > 0 else "")
                    + ("；" + "；".join(uncertain_why) if uncertain_why else "")),
            text=_text_between(sents, clip.t0, clip.end), evidence=ev,
        ))
        o = Output(
            id=f"clip_{rank}", segments=segs, width=size[0] if size else None, height=size[1] if size else None,
            captions=Captions(enabled=True, burn=True), reframe=_reframe_for(media, clip, size, reframe) if needs_reframe else None,
            selected=selected,
            title=_title(c, clip.t0, clip.end, sents), score=round(c.score), reasons=reasons,
        )
        if c.spam:
            o.reasons.append(f"原始分 {c.raw_score:.0f}，因刷屏扣到 {c.score:.0f}")
        outputs.append(o)
        log.info("候选 clip_%d %s–%s（%.1fs）分数 %.0f%s：%s", rank, mmss(clip.start), mmss(clip.end), clip.end - clip.start, c.score,
                 "" if selected else "（未选）", "；".join(reasons))

    if not outputs:
        raise RecipeError(
            f"{media.path.name} 里没有找到任何高光候选：没有音量峰、弹幕爆发或关键词。"
            "素材可能整段都很平，或者音轨不对（多音轨时用人声轨 / 混音轨），请检查素材"
        )
    notes.append(AUTH_NOTE)
    if size:
        if needs_reframe:
            how = ("裁切框跟着画面运动的重心走（帧差法，运动很少时保持居中）" if reframe == "track"
                   else "裁切框居中")
            notes.append(f"成片画幅 {size[0]}×{size[1]}（{aspect}），{how}；主体不在预期位置时在审阅页调整")
    notes.append("分数只用来排序，不代表播放量；每条的理由才是判断依据")

    plan = Plan(
        recipe="highlights", sources={sid: src}, outputs=outputs,
        options={"n": n, "candidates": cap, "length": [lmin, lmax], "aspect": aspect, "chat": str(chat) if chat else None,
                 "language": language, "reframe": reframe},
        notes=notes, path=plan_path,
    )
    finalize(plan)
    log.info("highlights 完成：%d 个候选，选中 %d 个，总耗时 %.2fs", len(outputs), len(chosen), time.perf_counter() - t_start)
    return plan


def _reframe_for(media: Media, clip: Clip, size: tuple[int, int] | None, mode: str) -> Reframe:
    v = media.info.video
    if mode == "track" and v and size:
        kfs = track_keyframes(media.path, clip.start, clip.end, v.display_aspect, size[0] / size[1])
        if kfs:
            return Reframe(mode="track", keyframes=kfs)
    return Reframe(mode="center")


def _how(how: str, is_start: bool) -> str:
    if how == "sentence":
        return "吸附到句首" if is_start else "吸附到句子 / 笑声结束之后"
    if how == "hard":
        return "硬切（没找到停顿）"
    return "落在停顿里"
