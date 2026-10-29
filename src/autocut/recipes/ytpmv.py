"""台词素材 + BGM → 鬼畜材料包 + 第一版对拍（"ytpmv"配方）。

Kiko 要的是材料，不是成品（F4）：
1. 把台词切成音节。有文字稿就用逐字时间（再按能量包络收紧边界）；没有就按能量包络里的谷切，并在 notes 里说明。
2. 在 BGM 的拍点 / 半拍上放音节，小节末重复最后一个字形成"鬼畜感"，这就是第一版对拍。
3. 全部音节另外列在 plan.options["materials"]，导出时按它出素材包。

每个放置点是一个 Segment：start/end = 音节在素材里的区间，slot = 到下一个放置点的间隔
（音节不够长就定格补齐，太长就截断），所以放置点精确落在拍 / 半拍上。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from autocut.analysis import SR, Media
from autocut.audio import frame_db
from autocut.plan import Bed, Output, Plan, Segment, new_segment_id
from autocut.recipes._common import (
    finalize,
    BEATS_PER_BAR,
    RecipeError,
    choose_music_start,
    fit_beat_grid,
    make_source,
    plan_bars,
    plan_path_for,
    unique_id,
)

log = logging.getLogger(__name__)

PATTERNS = ("auto", "beat", "half")
DEFAULT_BARS = 4
_HOP = 0.005  # 包络帧移
_WIN = 0.02
_VALLEY_DB = 7.0  # 两个音节之间的谷至少比两侧峰值低这么多 dB 才切开
_MIN_SYLL = 0.09  # 音节最短（秒）
_TIGHT_DB = 40.0  # 收紧边界：电平低于该音节峰值这么多 dB 的头尾去掉（放得宽一点，保住 p/t/k 这类弱辅音起音）
_REFINE = 0.1  # 文字稿时间最多向外/向内搜索这么远
_HALVES = 2 * BEATS_PER_BAR  # 每小节的半拍数
_PUNCT = " ，。！？、,.!?…~～；;：:\"'“”‘’（）()"
_CJK = re.compile(r"^[一-鿿぀-ヿ가-힯]+$")
# 简单旋律（相对原音高的半音数），按"新音节"依次取；重复的音节沿用被重复那个字的音高
MELODY = [0, 2, 4, 7, 9, 7, 4, 2]

# 每小节的放置模板：(半拍位置, 取第几个新音节)；同一序号出现两次 = 重复（鬼畜感）
_BAR_A = [(0, 0), (2, 1), (4, 2), (6, 3), (7, 3)]  # 四拍各一个字，最后一个字在末尾半拍重复
_BAR_B = [(0, 0), (2, 1), (3, 1), (4, 2), (6, 3), (7, 3)]  # 第二个字抢半拍重复一次，更跳
_BAR_BEAT = [(0, 0), (2, 1), (4, 2), (6, 3)]
_BAR_HALF = [(i, i) for i in range(_HALVES)]

FIRST_PASS_NOTE = ("这是第一版对拍，供在剪辑软件里继续编排：音节顺序、重复次数、变调和画面都建议自己再排，"
                   "全自动做出来的鬼畜容易'没那味儿'。")


@dataclass
class Syllable:
    text: str
    start: float
    end: float
    peak_db: float = 0.0
    origin: str = "words"  # words | energy


# ------------------------------------------------------------------ 切音节


def tighten(db: np.ndarray, a: float, b: float, lo: float, hi: float, connected: bool = True) -> tuple[float, float, float]:
    """把 [a, b] 收紧到真正有声的部分：在 [lo, hi] 内取电平 ≥ 峰值 - _TIGHT_DB 的范围。返回 (起, 止, 峰值 dB)。

    connected=True 只取与峰值相连的那一段（单个音节，邻近音节的尾巴不算）；False 取首尾两端（多个音节的词）。"""
    i0, i1 = max(int(lo / _HOP), 0), min(int(hi / _HOP) + 1, len(db))
    ia, ib = max(int(a / _HOP), i0), max(min(int(b / _HOP) + 1, i1), i0 + 1)
    seg = db[ia:ib] if ib > ia else db[i0:i1]
    if seg.size == 0:
        return a, b, -100.0
    peak = float(seg.max())
    floor = float(np.percentile(db, 10))
    thr = max(peak - _TIGHT_DB, floor + 8.0)
    hot = np.flatnonzero(db[i0:i1] >= thr)
    if hot.size == 0:
        return a, b, peak
    if not connected:
        return (i0 + int(hot[0])) * _HOP, (i0 + int(hot[-1]) + 1) * _HOP, peak
    pk = ia + int(np.argmax(seg)) - i0
    left = pk
    while left > 0 and db[i0 + left - 1] >= thr:
        left -= 1
    right = pk
    while right < i1 - i0 - 1 and db[i0 + right + 1] >= thr:
        right += 1
    return (i0 + left) * _HOP, (i0 + right + 1) * _HOP, peak


def split_valleys(db: np.ndarray, i0: int, i1: int, max_parts: int | None = None) -> list[tuple[int, int]]:
    """在帧区间 [i0, i1) 里递归地沿最深的谷切开：谷比两侧峰值低 ≥ _VALLEY_DB 且两边都不短于 _MIN_SYLL。

    只在"有声"的范围内找谷（电平 ≥ 区间峰值 - _TIGHT_DB），首尾的静音不算谷；谷底是一段平台时切在平台中间。"""
    min_f = int(_MIN_SYLL / _HOP)
    parts = [(i0, i1)]
    while True:
        best = None  # (depth, part_index, m)
        for pi, (a, b) in enumerate(parts):
            if b - a < 2 * min_f:
                continue
            seg = db[a:b]
            hot = np.flatnonzero(seg >= seg.max() - _TIGHT_DB)
            lo, hi = a + int(hot[0]) + min_f, a + int(hot[-1]) + 1 - min_f
            if hi <= lo:
                continue
            m = lo + int(np.argmin(db[lo:hi]))
            floor = db[m] + 0.5  # 谷底平台：向两侧扩到比最低点高 0.5dB 的地方，取中点
            l, r = m, m
            while l - 1 >= lo and db[l - 1] <= floor:
                l -= 1
            while r + 1 < hi and db[r + 1] <= floor:
                r += 1
            m = (l + r) // 2
            depth = min(float(db[a:m].max()), float(db[m:b].max())) - float(db[m])
            if depth >= _VALLEY_DB and (best is None or depth > best[0]):
                best = (depth, pi, m)
        if best is None or (max_parts is not None and len(parts) >= max_parts):
            return parts
        _, pi, m = best
        a, b = parts[pi]
        parts[pi : pi + 1] = [(a, m), (m, b)]


def _clean_tokens(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [w for w in words if str(w["text"]).strip(_PUNCT) and float(w["end"]) > float(w["start"])]


def _from_token(db: np.ndarray, toks: list[dict[str, Any]], i: int, duration: float) -> list[Syllable]:
    """单个词：按文字稿时间，在 ±_REFINE 内按能量收紧；多字词按谷切开，刚好切成字数份才拆。"""
    w = toks[i]
    a, b = float(w["start"]), float(w["end"])
    prev_end = float(toks[i - 1]["end"]) if i else 0.0
    next_start = float(toks[i + 1]["start"]) if i + 1 < len(toks) else duration
    lo = max(a - _REFINE, (prev_end + a) / 2 if prev_end > a - _REFINE else 0.0, 0.0)
    hi = min(b + _REFINE, (b + next_start) / 2 if next_start < b + _REFINE else duration, duration)
    text = str(w["text"]).strip()
    chars = list(text)
    s, e, peak = tighten(db, a, b, lo, hi, connected=len(chars) == 1)
    if e - s < 0.04:  # 收紧过头（数据怪），退回原始时间
        s, e = a, b
    if len(chars) > 1 and _CJK.match(text):
        parts = split_valleys(db, int(s / _HOP), int(e / _HOP), max_parts=len(chars))
        if len(parts) == len(chars):
            out = []
            for ch, (pa, pb) in zip(chars, parts):
                ts, te, pk = tighten(db, pa * _HOP, pb * _HOP, pa * _HOP, pb * _HOP)
                out.append(Syllable(ch, round(ts, 4), round(te, 4), pk))
            return out
    return [Syllable(text, round(s, 4), round(e, 4), peak)]


def syllables_from_words(
    words: list[dict[str, Any]], db: np.ndarray, duration: float, regions: tuple[tuple[float, float], ...] = ()
) -> list[Syllable]:
    """文字稿 → 音节。

    语音识别给的逐字时间常常松（相邻字首尾相接、整体偏移 0.1–0.2s），直接拿来切会切到邻字。
    所以先看能量包络：一个语音区间里，按谷切出的段数正好等于文字稿的汉字数，就一一对应（边界取能量的，文字取识别的）；
    对不上（连读、英文、识别出错）就退回文字稿时间，只在 ±0.1s 内按能量收紧。
    """
    toks = _clean_tokens(words)
    out: dict[int, list[Syllable]] = {}
    claimed: set[int] = set()
    for ra, rb in regions:
        idx = [i for i, w in enumerate(toks) if ra - 0.3 <= (float(w["start"]) + float(w["end"])) / 2 <= rb + 0.3 and i not in claimed]
        if not idx or not all(_CJK.match(str(toks[i]["text"]).strip()) for i in idx):
            continue
        chars = [(i, ch) for i in idx for ch in str(toks[i]["text"]).strip()]
        parts = split_valleys(db, int(ra / _HOP), min(int(rb / _HOP) + 1, len(db)))
        tight = [tighten(db, pa * _HOP, pb * _HOP, pa * _HOP, pb * _HOP) for pa, pb in parts]
        tight = [t for t in tight if t[1] - t[0] >= _MIN_SYLL * 0.8]
        if len(tight) != len(chars):
            continue
        for (i, ch), (ts, te, pk) in zip(chars, tight):
            out.setdefault(i, []).append(Syllable(ch, round(ts, 4), round(te, 4), pk))
        claimed.update(idx)
    for i in range(len(toks)):
        if i not in claimed:
            out[i] = _from_token(db, toks, i, duration)
    return [s for i in sorted(out) for s in out[i]]


def syllables_from_energy(db: np.ndarray, regions: tuple[tuple[float, float], ...]) -> list[Syllable]:
    """没有文字稿：每个语音区间内按能量包络的谷切音节，text 留空。"""
    out: list[Syllable] = []
    for a, b in regions:
        for pa, pb in split_valleys(db, int(a / _HOP), min(int(b / _HOP) + 1, len(db))):
            s, e, pk = tighten(db, pa * _HOP, pb * _HOP, pa * _HOP, pb * _HOP)
            if e - s >= _MIN_SYLL * 0.8:
                out.append(Syllable("", round(s, 4), round(e, 4), pk, origin="energy"))
    return out


# ------------------------------------------------------------------ 对拍


def bar_templates(pattern: str, bars: int) -> list[list[tuple[int, int]]]:
    if pattern == "beat":
        return [_BAR_BEAT] * bars
    if pattern == "half":
        return [_BAR_HALF] * bars
    if pattern == "auto":
        return [(_BAR_B if (j % 2 == 1 and j != bars - 1) else _BAR_A) for j in range(bars)]
    raise RecipeError(f"pattern={pattern!r}：应为 {PATTERNS} 之一（auto 四拍一字 + 小节末重复；beat 每拍一字；half 每个半拍一字）")


def pick_run(sylls: list[Syllable], need: int) -> list[Syllable]:
    """音节够用就全用；太多就取连续 need 个里最响的一段（响 = 清晰，切出来更有冲击力）。"""
    if len(sylls) <= need:
        return sylls
    peaks = np.array([s.peak_db for s in sylls])
    scores = [peaks[i : i + need].mean() for i in range(len(sylls) - need + 1)]
    i = int(np.argmax(scores))
    return sylls[i : i + need]


# ------------------------------------------------------------------ 配方入口


def build(
    sources: list[Path],
    *,
    out: Path | None = None,
    music: Path | str | None = None,
    pattern: str = "auto",
    pitch: bool = False,
    length: float | None = None,
    language: str | None = None,
) -> Plan:
    """台词素材 + BGM → 鬼畜材料包 + 第一版对拍方案。

    music：BGM 路径（必需）；pattern：auto / beat / half；pitch：True 时按简单旋律给音节变调（Segment.pitch 半音）；
    length：目标秒数，吸附到整小节，默认 4 小节；language：交给语音识别的语言。
    """
    t_start = time.perf_counter()
    if music is None:
        raise RecipeError("ytpmv 需要 BGM：build([台词素材], music=Path('bgm.wav'))；对拍的节奏从它来")
    if not sources:
        raise RecipeError("ytpmv 需要一个带台词的素材（视频或音频）")
    music_path = Path(music)
    if not music_path.exists():
        raise RecipeError(f"找不到 BGM 文件 {music_path}")

    media = Media(sources[0])
    if not media.info.audio:
        raise RecipeError(f"{media.path} 没有音轨，没法从里面切音节；请换一个有台词的素材")
    notes: list[str] = []
    if len(sources) > 1:
        notes.append(f"ytpmv 一次只用一个台词素材，用了 {media.path.name}，其余 {len(sources) - 1} 个没有用")
    plan_path = plan_path_for(media.path, out)

    # ---- 节拍
    mus = Media(music_path)
    grid = fit_beat_grid(mus.beats(), mus.duration, source_name=music_path.name)
    if length is None:
        bars, bars_note = DEFAULT_BARS, f"默认 {DEFAULT_BARS} 小节 = {DEFAULT_BARS * grid.bar:.2f}s（{grid.bpm:g} BPM）"
    else:
        bars, bars_note = plan_bars(float(length), grid)
    if bars > grid.n_bars:
        notes.append(f"曲子只有 {grid.n_bars} 个完整小节，不够 {bars} 个，缩到 {grid.n_bars} 小节")
        bars = grid.n_bars
    if bars < 1:
        raise RecipeError(f"{music_path.name} 不足一个完整小节（{mus.duration:.1f}s，{grid.bpm:g} BPM），不能对拍")
    notes.append(bars_note)
    j0, start_note = choose_music_start(mus, grid, bars)
    music_in = grid.bar_start(j0)
    out_dur = bars * grid.bar
    half = grid.period / 2

    # ---- 音节
    x = media.audio()
    db = frame_db(x, SR, _HOP, _WIN)
    words = media.words(language=language)
    if words:
        sylls = syllables_from_words(words, db, media.duration, media.speech().regions)
        origin = "words"
    else:
        sylls = syllables_from_energy(db, media.speech().regions)
        origin = "energy"
        notes.append("没有文字稿（语音识别不可用）：音节是按声音能量的起伏切的，text 为空，字与音节的对应、"
                     "多字连读的切分可能不准；装好语音识别后重跑，音节会带上文字、边界更准")
    if not sylls:
        raise RecipeError(
            f"{media.path.name} 里没切出任何音节：没检测到语音。确认素材里有清晰的台词（背景音乐太响也会干扰），"
            "或者换一个素材"
        )
    log.info("音节：%d 个（%s），素材 %.1fs", len(sylls), "文字稿逐字" if origin == "words" else "能量包络", media.duration)

    # ---- 第一版对拍
    templates = bar_templates(pattern, bars)
    need = sum(max(i for _, i in t) + 1 for t in templates)
    run = pick_run(sylls, need)
    if len(run) < need:
        notes.append(f"素材里只有 {len(run)} 个音节，不够 {need} 个，按顺序循环使用")
    sid = unique_id(media.path.stem, {"music"})
    srcs = {sid: make_source(sid, media, plan_path), "music": make_source("music", mus, plan_path, kind="audio")}

    segs: list[Segment] = []
    new_idx = 0  # 已经用掉的"新音节"个数
    for j, tpl in enumerate(templates):
        base = new_idx
        for k, (pos, which) in enumerate(tpl):
            nxt = tpl[k + 1][0] if k + 1 < len(tpl) else _HALVES
            syl = run[(base + which) % len(run)]
            repeat = k > 0 and which == tpl[k - 1][1]
            melody = MELODY[(base + which) % len(MELODY)] if pitch else 0.0
            beat_no, off = pos // 2 + 1, pos % 2
            where = f"第 {j + 1} 小节第 {beat_no} 拍" + ("后半拍" if off else "")
            label = f"「{syl.text}」" if syl.text else f"第 {(sylls.index(syl) + 1)} 个音节"
            segs.append(Segment(
                id=new_segment_id("y", len(segs) + 1), source=sid, start=syl.start, end=syl.end, reason="syllable",
                confidence=0.9 if syl.origin == "words" else 0.6,
                detail=f"{where} · {label}" + ("（重复，鬼畜点）" if repeat else "")
                       + (f" · 变调 {melody:+g} 半音" if pitch else ""),
                text=syl.text, slot=round((nxt - pos) * half, 6), pitch=float(melody),
                evidence={"bar": j + 1, "half_beat": pos, "repeat": repeat, "from": syl.origin},
            ))
        new_idx += max(i for _, i in tpl) + 1

    materials = [
        {"id": f"s{i:03d}", "source": sid, "start": s.start, "end": s.end, "text": s.text, "from": s.origin}
        for i, s in enumerate(sylls, 1)
    ]
    n_used = len({(s.start, s.end) for s in segs})
    out_obj = Output(
        id="ytpmv", segments=segs,
        beds=[Bed(source="music", at=0.0, start=round(music_in, 4), end=round(music_in + out_dur, 4), role="music")],
        title=f"鬼畜第一版 · {grid.bpm:g} BPM",
        reasons=[f"{len(segs)} 次放置（{n_used} 个不同音节）在拍 / 半拍上", f"素材里共切出 {len(sylls)} 个音节，全部在 materials 里"],
    )
    notes += [f"BGM {start_note}", FIRST_PASS_NOTE]
    plan = Plan(
        recipe="ytpmv", sources=srcs, outputs=[out_obj],
        options={"music": str(music_path), "pattern": pattern, "pitch": pitch, "length": length, "bpm": grid.bpm, "bars": bars,
                 "music_start": round(music_in, 4), "materials": materials, "syllables_from": origin},
        notes=notes, path=plan_path,
    )
    finalize(plan)
    log.info("ytpmv 完成：%d 次放置 / %d 个音节 / %d 小节 / %.2fs，总耗时 %.2fs", len(segs), len(sylls), bars, out_dur, time.perf_counter() - t_start)
    return plan
