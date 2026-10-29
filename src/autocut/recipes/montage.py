"""若干素材片段 + BGM → 卡点混剪（"montage"配方）。

方案里不放任何"差一点点"的数：先把检测出的拍点拟合成等距网格（BPM 吸附到整数），
每个镜头的 slot = 拍间隔的整数倍，累加起来精确落在拍上；输出时长 = 整数个小节。

镜头库：每个素材用 media.cuts() 切成镜头，再按 0.25s 步长滑出候选窗口，给每个窗口算
平均颜色 / 亮度 / 运动量。挑镜头时让相邻镜头的视觉差异尽量大，同一段画面不重复，
并保证用到尽可能多的不同素材。
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autocut.analysis import Media
from autocut.plan import Bed, Output, Plan, Reframe, Segment, new_segment_id
from autocut.recipes._common import (
    finalize,
    BEATS_PER_BAR,
    BeatGrid,
    RecipeError,
    aspect_size,
    choose_music_start,
    fit_beat_grid,
    make_source,
    plan_bars,
    plan_path_for,
    unique_id,
)

log = logging.getLogger(__name__)

CUT_EVERY = ("beat", "2beats", "bar", "auto")
_STEP = 0.25  # 候选窗口的滑动步长（秒）
_CUT_MARGIN = 0.06  # 窗口离镜头切点至少这么远，避免带进上一个镜头的第一帧或闪白
_THUMB = (32, 18)
_MIN_DISTINCT = 4  # 至少用到几个不同的素材（素材够的话）
_MAX_SAMPLES = 2400  # 每个素材最多采这么多帧做描述，长素材降低采样率


@dataclass
class Shot:
    """镜头库里的一个候选窗口。"""

    src: int  # 素材序号
    start: float
    end: float
    rgb: np.ndarray  # 平均颜色 0–255
    luma: float
    motion: float
    best_in_shot: bool = False
    freeze: bool = False  # 镜头比 slot 短，要靠定格补齐


@dataclass
class Described:
    """一个素材的低分辨率逐帧描述。"""

    fps: float
    rgb: np.ndarray  # (N, 3) 每个采样帧的平均颜色
    luma: np.ndarray  # (N,)
    motion: np.ndarray  # (N,) 与上一采样帧的平均绝对差 / 255；跨镜头切点处置 0

    def window(self, a: float, b: float) -> tuple[np.ndarray, float, float]:
        n = len(self.luma)
        i0 = min(max(int(np.floor(a * self.fps)), 0), n - 1)
        i1 = min(max(int(np.ceil(b * self.fps)), i0 + 1), n)
        return self.rgb[i0:i1].mean(axis=0), float(self.luma[i0:i1].mean()), float(self.motion[i0:i1].mean())


# ------------------------------------------------------------------ 镜头描述


def describe_video(path: Path, duration: float, cuts: list[float]) -> Described:
    """ffmpeg 解成 32×18 的小图，算每帧平均颜色、亮度和帧间运动量。"""
    fps = float(min(max(_MAX_SAMPLES / max(duration, 1e-6), 1.0), 8.0))
    w, h = _THUMB
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-an",
        "-vf", f"fps={fps},scale={w}:{h}:flags=area", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RecipeError(f"ffmpeg 解不开 {path}：{r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    size = w * h * 3
    n = len(r.stdout) // size
    if n == 0:
        raise RecipeError(f"{path} 没有解出任何画面帧；请确认它是有视频流的文件")
    frames = np.frombuffer(r.stdout, dtype=np.uint8, count=n * size).reshape(n, h, w, 3).astype(np.float32)
    rgb = frames.mean(axis=(1, 2))
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    motion = np.zeros(n, dtype=np.float32)
    if n > 1:
        motion[1:] = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2, 3)) / 255.0
    ts = (np.arange(n) + 0.5) / fps
    for c in cuts:  # 切点前后两个采样帧的差不是"运动"
        j = int(np.searchsorted(ts, c))
        for k in (j, j + 1):
            if 0 <= k < n:
                motion[k] = 0.0
    log.debug("描述 %s：%d 帧 @%.1ffps，耗时 %.2fs", path.name, n, fps, time.perf_counter() - t0)
    return Described(fps=fps, rgb=rgb, luma=luma, motion=motion)


def shot_bounds(duration: float, cuts: list[float]) -> list[tuple[float, float]]:
    pts = [0.0] + [c for c in cuts if 0.2 < c < duration - 0.2] + [duration]
    return [(a, b) for a, b in zip(pts[:-1], pts[1:]) if b - a >= 0.3]


def windows_for(slot: float, shots_by_src: list[list[tuple[float, float]]], desc: list[Described]) -> list[Shot]:
    """所有素材里长度为 slot 的候选窗口，镜头比 slot 短的（≥ 0.6·slot）补一个整镜头窗口、标记定格。"""
    out: list[Shot] = []
    ends = [shots[-1][1] if shots else 0.0 for shots in shots_by_src]  # 素材结尾：结尾处不留切点边距
    for si, shots in enumerate(shots_by_src):
        for a, b in shots:
            lo = a + (_CUT_MARGIN if a > 0 else 0.0)
            hi = b - (_CUT_MARGIN if b < ends[si] - 1e-3 else 0.0)
            ws: list[Shot] = []
            if hi - lo >= slot - 1e-6:
                starts = list(np.arange(lo, hi - slot + 1e-6, _STEP))
                if not starts or abs(starts[-1] - (hi - slot)) > 1e-6:
                    starts.append(hi - slot)
                for s in starts:
                    rgb, luma, mot = desc[si].window(s, s + slot)
                    ws.append(Shot(si, float(s), float(s + slot), rgb, luma, mot))
            elif hi - lo >= 0.6 * slot:
                rgb, luma, mot = desc[si].window(lo, hi)
                ws.append(Shot(si, float(lo), float(hi), rgb, luma, mot, freeze=True))
            if ws:
                best = max(w.motion for w in ws)
                for w in ws:
                    w.best_in_shot = w.motion >= best - 1e-9
                out += ws
    return out


def visual_distance(a: Shot, b: Shot) -> float:
    """两个窗口的视觉差异 ∈ [0, 1]：颜色 60% + 亮度 20% + 运动量 20%。"""
    color = float(np.linalg.norm(a.rgb - b.rgb)) / (255.0 * np.sqrt(3))
    luma = abs(a.luma - b.luma) / 255.0
    mot = min(abs(a.motion - b.motion) / 0.2, 1.0)
    return 0.6 * min(color * 2.0, 1.0) + 0.2 * min(luma * 2.0, 1.0) + 0.2 * mot


# ------------------------------------------------------------------ 节拍与小节


def cut_pattern(bars: int, cut_every: str) -> list[int]:
    """每个镜头占几拍。auto：前面每 2 拍切一次，最后一小节每拍切一次（加速感）。"""
    per_bar = BEATS_PER_BAR
    if cut_every == "beat":
        return [1] * (bars * per_bar)
    if cut_every == "2beats":
        return [2] * (bars * per_bar // 2)
    if cut_every == "bar":
        return [per_bar] * bars
    if cut_every == "auto":
        return [2] * ((bars - 1) * per_bar // 2) + [1] * per_bar
    raise RecipeError(f"cut_every={cut_every!r}：应为 {CUT_EVERY} 之一（beat 每拍切、2beats 每两拍、bar 每小节、auto 前慢后快）")


# ------------------------------------------------------------------ 选镜头


def pick_shots(
    slots_beats: list[int],
    grid: BeatGrid,
    shots_by_src: list[list[tuple[float, float]]],
    desc: list[Described],
    names: list[str],
) -> tuple[list[Shot], list[float], list[str]]:
    """逐个 slot 贪心选窗口。返回 (选中的窗口, 与上一镜头的视觉差异, 备注)。

    得分 = 0.45·与上一镜头的差异 + 0.30·运动量 + 0.20·没用过的素材 − 0.15·该素材已用占比 − 0.4·与上一镜头同素材。
    硬约束：同一素材里窗口不重叠；剩余镜头数不够补齐 _MIN_DISTINCT 个素材时，必须选新素材。
    """
    n_src = len(shots_by_src)
    want_distinct = min(_MIN_DISTINCT, n_src)
    used: dict[int, list[tuple[float, float]]] = {i: [] for i in range(n_src)}
    uses = [0] * n_src
    chosen: list[Shot] = []
    divs: list[float] = []
    notes: list[str] = []
    cache: dict[float, list[Shot]] = {}
    reused = 0
    for i, beats in enumerate(slots_beats):
        slot = round(beats * grid.period, 6)
        if slot not in cache:
            cache[slot] = windows_for(slot, shots_by_src, desc)
            if not cache[slot]:
                raise RecipeError(
                    f"没有任何素材能提供 {slot:.2f}s 的连续画面（所有镜头都比这短）；"
                    f"请换 cut_every（现在每个镜头 {beats} 拍 = {slot:.2f}s）或提供更长的素材"
                )
        allw = cache[slot]
        mmax = max(max(w.motion for w in allw), 1e-6)

        def free(w: Shot) -> bool:
            return all(w.end <= a + 1e-6 or w.start >= b - 1e-6 for a, b in used[w.src])

        pool = [w for w in allw if free(w)]
        overlapped = False
        if not pool:  # 素材总长不够：放开"不重叠"，只剔除和上一镜头同一段的
            overlapped = True
            pool = list(allw)
        n_used = sum(1 for u in uses if u)
        remaining = len(slots_beats) - i
        if n_used < want_distinct and remaining <= want_distinct - n_used:
            fresh = [w for w in pool if uses[w.src] == 0]
            pool = fresh or pool
        prev = chosen[-1] if chosen else None

        def score(w: Shot) -> tuple[float, float]:
            div = visual_distance(prev, w) if prev else 0.5
            s = (0.45 * div + 0.30 * min(w.motion / mmax, 1.0) + 0.20 * (1.0 if uses[w.src] == 0 else 0.0)
                 - 0.15 * (uses[w.src] / (i + 1)) - (0.4 if prev and prev.src == w.src else 0.0)
                 - (0.3 if w.freeze else 0.0) - (0.5 if overlapped else 0.0))
            return s, div

        best = max(pool, key=lambda w: (score(w)[0], -w.start))
        chosen.append(best)
        divs.append(score(best)[1])
        used[best.src].append((best.start, best.end))
        uses[best.src] += 1
        if overlapped:
            reused += 1
    if reused:
        notes.append(f"素材总长不够：有 {reused} 个镜头只能重复使用已经用过的画面（建议再加几段素材）")
    short = sum(1 for w in chosen if w.freeze)
    if short:
        notes.append(f"有 {short} 个镜头所在的素材比 slot 短，靠定格补齐")
    n_used = len({w.src for w in chosen})
    if n_used < want_distinct:
        notes.append(f"只用到了 {n_used} 个素材（目标 ≥ {want_distinct}）：其余素材的镜头都比 slot 短或不够用")
    return chosen, divs, notes


# ------------------------------------------------------------------ 配方入口


def build(
    sources: list[Path],
    *,
    out: Path | None = None,
    music: Path | str | None = None,
    length: float = 15.0,
    cut_every: str = "auto",
    aspect: str | None = None,
    music_start: float | None = None,
) -> Plan:
    """若干素材片段 + BGM → 卡点混剪方案。

    music：BGM 路径（必需）；length：目标秒数，吸附到整小节；cut_every：beat / 2beats / bar / auto；
    aspect：成片画幅（None 跟随第一个素材）；music_start：从音乐的第几秒开始（吸附到小节首拍），None 自动选最响的一段。
    """
    t_start = time.perf_counter()
    if music is None:
        raise RecipeError("montage 需要 BGM：build(clips, music=Path('bgm.wav'))；节拍从这首曲子里检测")
    if not sources:
        raise RecipeError("montage 需要至少一个视频素材")
    music_path = Path(music)
    if not music_path.exists():
        raise RecipeError(f"找不到 BGM 文件 {music_path}")
    if cut_every not in CUT_EVERY:
        raise RecipeError(f"cut_every={cut_every!r}：应为 {CUT_EVERY} 之一")

    plan_path = plan_path_for(Path(sources[0]), out)
    mus = Media(music_path)
    grid = fit_beat_grid(mus.beats(), mus.duration, source_name=music_path.name)
    bars, bars_note = plan_bars(float(length), grid)
    notes = [bars_note]
    if bars > grid.n_bars:
        notes.append(f"曲子只有 {grid.n_bars} 个完整小节，不够 {bars} 个，成片缩到 {grid.n_bars} 小节")
        bars = grid.n_bars
    if bars < 1:
        raise RecipeError(f"{music_path.name} 不足一个完整小节（{mus.duration:.1f}s，{grid.bpm:g} BPM），不能混剪")
    if music_start is not None:
        j0 = int(np.clip(round((music_start - grid.bar_start(0)) / grid.bar), 0, max(grid.n_bars - bars, 0)))
        start_note = f"从第 {j0 + 1} 小节（{grid.bar_start(j0):.2f}s）开始（music_start={music_start:g} 吸附到小节首拍）"
    else:
        j0, start_note = choose_music_start(mus, grid, bars)
    notes.append("BGM " + start_note)
    music_in = grid.bar_start(j0)
    out_dur = bars * grid.bar

    # ---- 镜头库
    medias = [Media(p) for p in sources]
    for m in medias:
        if m.info.video is None:
            raise RecipeError(f"{m.path} 没有视频流，不能当混剪镜头；混剪素材需要是视频文件")
    t_lib = time.perf_counter()
    shots_by_src, desc = [], []
    for m in medias:
        cuts = m.cuts()
        shots_by_src.append(shot_bounds(m.duration, cuts))
        desc.append(describe_video(m.path, m.duration, cuts))
    log.info("镜头库：%d 个素材，%d 个镜头，耗时 %.2fs", len(medias), sum(len(s) for s in shots_by_src), time.perf_counter() - t_lib)

    ids: list[str] = []
    srcs = {}
    for m in medias:
        sid = unique_id(m.path.stem, set(srcs) | {"music"})
        ids.append(sid)
        srcs[sid] = make_source(sid, m, plan_path)
    srcs["music"] = make_source("music", mus, plan_path, kind="audio")

    pattern = cut_pattern(bars, cut_every)
    chosen, divs, pick_notes = pick_shots(pattern, grid, shots_by_src, desc, ids)
    notes += pick_notes

    # ---- 方案
    segs: list[Segment] = []
    beat_i = 0
    for i, (beats, w, div) in enumerate(zip(pattern, chosen, divs), 1):
        bar_no, beat_no = beat_i // BEATS_PER_BAR + 1, beat_i % BEATS_PER_BAR + 1
        slot = beats * grid.period
        mot = "运动最强" if w.best_in_shot else ("画面较静" if w.motion < 0.01 else "运动中等")
        what = f"{ids[w.src]} {mot}的 {slot:g}s" if w.best_in_shot else f"{ids[w.src]} {w.start:.2f}s 起的 {slot:g}s（{mot}）"
        segs.append(Segment(
            id=new_segment_id("m", i), source=ids[w.src], start=round(w.start, 4), end=round(w.end, 4),
            reason="beat" if beat_no == 1 else "shot",
            confidence=round(float(np.clip(0.7 + 0.3 * div, 0.5, 0.95)) * (0.7 if w.freeze else 1.0), 2),
            detail=f"第 {bar_no} 小节第 {beat_no} 拍 · {what}" + ("，镜头不够长，定格补齐" if w.freeze else ""),
            slot=round(slot, 6), mute=True,
            evidence={"bar": bar_no, "beat": beat_no, "visual_diff": round(div, 2), "motion": round(w.motion, 3)},
        ))
        beat_i += beats

    size = aspect_size(aspect)
    first_v = medias[0].info.video
    reframe = None
    if size and first_v and abs(size[0] / size[1] - first_v.display_aspect) > 0.02:
        reframe = Reframe(mode="center")
        notes.append(f"成片 {size[0]}×{size[1]}（{aspect}），裁切框居中；主体不在中间的镜头在审阅页调整")
    sizes = {(m.info.video.width, m.info.video.height) for m in medias}
    if size is None and len(sizes) > 1:
        v0 = medias[0].info.video
        size = (v0.width, v0.height)
        notes.append(f"素材分辨率不一致（{sorted(sizes)}），统一成第一个素材的 {size[0]}×{size[1]}")

    n_used = len({w.src for w in chosen})
    out_obj = Output(
        id="montage", segments=segs, width=size[0] if size else None, height=size[1] if size else None,
        beds=[Bed(source="music", at=0.0, start=round(music_in, 4), end=round(music_in + out_dur, 4), role="music")],
        reframe=reframe, title=f"卡点混剪 · {grid.bpm:g} BPM",
        reasons=[f"{grid.bpm:g} BPM · {bars} 小节 · {out_dur:.1f}s", f"{len(segs)} 个镜头，来自 {n_used}/{len(medias)} 个素材"],
    )
    notes.append("镜头自带的声音全部静音，成片音频就是 BGM；每个镜头可以在审阅页单独换掉")
    plan = Plan(
        recipe="montage", sources=srcs, outputs=[out_obj],
        options={"music": str(music_path), "length": length, "cut_every": cut_every, "aspect": aspect, "bpm": grid.bpm,
                 "bars": bars, "music_start": round(music_in, 4)},
        notes=notes, path=plan_path,
    )
    finalize(plan)
    log.info("montage 完成：%d 个镜头 / %d 个素材 / %d 小节 / %.2fs，总耗时 %.2fs", len(segs), n_used, bars, out_dur, time.perf_counter() - t_start)
    return plan
