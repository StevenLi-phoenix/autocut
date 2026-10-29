"""出片：把方案（Plan）的一个输出变成 mp4，并立刻实测成品（时长、音画漂移），差异写进 warnings，不静默。

设计要点
- **帧格对齐**：每个片段的输出帧数按累计时间取整（总误差 ≤ 半帧，不随片段数累积），音频长度取同一个帧数对应的时长，
  所以音画天然同步；时长与方案的差 ≤ 1 帧。
- **每个片段一个 `-ss` 输入块**：长直播切几个高光时只解码用到的部分；同一素材里相邻、顺序递增的片段合并成一个块（split + trim）。
- **每个剪口两侧淡入淡出**（audio_fade_ms），首尾相接的连续片段不加，避免接缝处凹下去。
- 滤镜图写成脚本文件（`-/filter_complex`）；字幕烧录用生成的 ASS（字号按输出分辨率算，不依赖 libass 的 288 行默认画布）。
- 响度标准化走两遍：先只跑音频测 I/TP/LRA，再用测量值做线性 loudnorm（不会"泵"）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

from autocut.captions import Cue, cues_for, format_srt
from autocut.plan import Bed, Output, Overlay, Plan, Segment
from autocut.probe import MediaInfo, probe

log = logging.getLogger(__name__)

AUDIO_SR = 48000
LOUDNESS_TARGET = -14.0  # 竖屏短视频默认响度（LUFS）
TRUE_PEAK = -1.5
CHUNK_GAP = 20.0  # 同一素材里相隔不超过这么多秒的顺序片段共用一个解码块（再远就重新 seek）
VIDEO_ENCODER = os.environ.get("AUTOCUT_ENCODER", "libx264")  # 也可设 h264_videotoolbox（快，画质/码率控制略逊）
CJK_FONTS = (  # 字幕/文字的中文字体，按顺序找第一个存在的（macOS）
    ("Hiragino Sans GB", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
    ("Heiti SC", "/System/Library/Fonts/STHeiti Medium.ttc"),
    ("PingFang SC", "/System/Library/Fonts/PingFang.ttc"),
)
_STD_FPS = (24000 / 1001, 24, 25, 30000 / 1001, 30, 50, 60000 / 1001, 60)
_ZOOM = 0.10  # Ken Burns 缩放幅度

ProgressFn = Callable[[str, float], None]


class RenderError(RuntimeError):
    """出片失败。消息里说明哪个输出、哪个素材/字段出了问题，以及 ffmpeg 的关键报错。"""


@dataclass
class RenderResult:
    output_id: str
    path: Path
    expected_duration: float  # 方案算出来的输出时长
    duration: float | None  # 成品实测时长（dry_run 为 None）
    av_drift: float | None  # |视频流时长 - 音频流时长|（秒）
    srt_path: Path | None
    command: list[str]  # ffmpeg 参数列表（最终编码那一遍）
    warnings: list[str] = field(default_factory=list)
    filter_graph: str = ""  # 滤镜图脚本全文，便于排查
    loudness: dict | None = None  # 响度标准化的测量/目标（没做则 None）


# ================================================================== 共用：帧率、尺寸、帧格


def default_stem(plan: Plan) -> str:
    """方案文件名去掉 .json 和 .autocut：talk.autocut.json → talk。"""
    if plan.path is None:
        return "autocut"
    name = plan.path.name
    for suf in (".json", ".autocut"):
        if name.endswith(suf):
            name = name[: -len(suf)]
    return name or "autocut"


def snap_fps(x: float) -> Fraction:
    """把探测到的帧率吸附到标准值（29.97 → 30000/1001），可变帧率素材的平均帧率也走这里得到恒定帧率。"""
    if x <= 0:
        return Fraction(30)
    best = min(_STD_FPS, key=lambda s: abs(s - x))
    if abs(best - x) / best < 0.02:
        return Fraction(30000, 1001) if abs(best - 30000 / 1001) < 1e-6 else (
            Fraction(24000, 1001) if abs(best - 24000 / 1001) < 1e-6 else (
                Fraction(60000, 1001) if abs(best - 60000 / 1001) < 1e-6 else Fraction(best).limit_denominator(1)))
    return Fraction(min(max(round(x), 1), 60))


def _display_size(info: MediaInfo) -> tuple[int, int]:
    """显示尺寸：先按旋转交换宽高，再按显示宽高比（含 SAR）算宽。手机竖拍 1920×1080+rotation 90 → 1080×1920。"""
    v = info.video
    if v is None:
        return (0, 0)
    h = v.width if v.rotation in (90, 270) else v.height
    w = round(h * v.display_aspect) if v.display_aspect else (v.height if v.rotation in (90, 270) else v.width)
    return (w, h)


def _main_source(plan: Plan, output: Output) -> str | None:
    kept = output.kept()
    for s in kept:
        if plan.sources[s.source].kind == "video":
            return s.source
    for s in kept:
        if plan.sources[s.source].kind == "image":
            return s.source
    return next((k for k, v in plan.sources.items() if v.kind == "video"), None)


def _info(plan: Plan, source_id: str) -> MediaInfo:
    p = plan.source_path(source_id)
    if not p.exists():
        raise RenderError(f"素材文件不存在：{p}（sources.{source_id}.path={plan.sources[source_id].path!r}）。检查路径，相对路径以方案文件所在目录为基准")
    return probe(p)


def output_fps(plan: Plan, output: Output) -> Fraction:
    if output.fps:
        return snap_fps(output.fps)
    main = _main_source(plan, output)
    if main is not None and plan.sources[main].kind == "video":
        info = _info(plan, main)
        if info.video and info.video.fps > 0:
            return snap_fps(info.video.fps)
    return Fraction(30)


def output_size(plan: Plan, output: Output) -> tuple[int, int]:
    main = _main_source(plan, output)
    dw, dh = (1280, 720)
    if main is not None:
        w, h = _display_size(_info(plan, main))
        if w and h:
            dw, dh = w, h
    w, h = output.width, output.height
    if w and not h:
        h = round(w * dh / dw)
    elif h and not w:
        w = round(h * dw / dh)
    elif not w and not h:
        w, h = dw, dh
    return (int(w) // 2 * 2, int(h) // 2 * 2)


def frame_grid(output: Output, fps: Fraction) -> list[tuple[Segment, int, int]]:
    """保留片段在输出帧格上的位置 [(segment, 起始帧, 结束帧)]：按累计时间取整，误差不累积；每段至少 1 帧。"""
    grid, t, f0 = [], 0.0, 0
    for seg in output.kept():
        t += seg.out_duration
        f1 = max(int(math.floor(t * float(fps) + 0.5)), f0 + 1)
        grid.append((seg, f0, f1))
        f0 = f1
    return grid


# ================================================================== 滤镜图构造


def _n(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def _q(path: str | Path) -> str:
    """滤镜参数里的路径：单引号包起来，转义 \\ ' :（我们自己建的工作目录，路径不会有奇怪字符，这里只是兜底）。"""
    s = str(path).replace("\\", "\\\\").replace("'", "'\\\\\\''").replace(":", "\\:")
    return f"'{s}'"


def _atempo(speed: float) -> str:
    parts, f = [], speed
    while f > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        f /= 0.5
    if abs(f - 1.0) > 1e-9:
        parts.append(f"atempo={_n(f)}")
    return ",".join(parts)


def _lerp_expr(points: list[tuple[float, float]], t: str) -> str:
    """分段线性插值的 ffmpeg 表达式；t 是表达式里的时间变量。首尾之外取端点值。"""
    pts = sorted(points)
    if len(pts) == 1:
        return _n(pts[0][1])
    expr = _n(pts[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        seg = f"{_n(v0)}+({_n(v1 - v0)})*({t}-{_n(t0)})/{_n(max(t1 - t0, 1e-6))}"
        expr = f"if(lt({t},{_n(t1)}),{seg},{expr})"
    return f"if(lt({t},{_n(pts[0][0])}),{_n(pts[0][1])},{expr})"


def _kenburns(w: int, h: int, n: int, fps: Fraction, k: int) -> str:
    """单帧输入 → n 帧的缓慢推拉（偶数序号推近、奇数拉远，带一点平移）。先放大到 2 倍再 zoompan，避免整数取整造成的抖动。"""
    n = max(n, 2)
    z = f"1+{_ZOOM}*on/{n}" if k % 2 == 0 else f"1+{_ZOOM}-{_ZOOM}*on/{n}"
    px = f"0.35+0.3*on/{n}" if k % 4 < 2 else f"0.65-0.3*on/{n}"
    return (
        f"scale={2 * w}:{2 * h}:force_original_aspect_ratio=decrease,pad={2 * w}:{2 * h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"zoompan=z='{z}':x='(iw-iw/zoom)*({px})':y='(ih-ih/zoom)/2':d={n}:s={w}x{h}:fps={fps.numerator}/{fps.denominator},"
        f"setsar=1,format=yuv420p"
    )


@dataclass
class _Src:
    path: Path
    kind: str
    info: MediaInfo
    n_audio: int
    track: int  # 输出用的音轨序号
    disp: tuple[int, int]
    sar_fix: bool  # 非方形像素：先把宽按显示宽高比展开


@dataclass
class _Chunk:
    src: str
    ss: float
    end: float
    last_start: float
    members: list[int]
    idx: int = -1


@dataclass
class _Build:
    inputs: list[list[str]]
    video: list[str]  # 视频部分的滤镜行
    audio: list[str]  # 音频部分的滤镜行（到 [apre] 为止，响度标准化在外面接）
    size: tuple[int, int]
    fps: Fraction
    total_frames: int
    total: float  # 帧格对齐后的输出时长（秒）
    files: dict[str, str]  # 工作目录里要写的辅助文件：名字 → 内容
    warnings: list[str]
    cues: list[Cue]
    burn: bool


def _font() -> tuple[str, str] | None:
    for name, path in CJK_FONTS:
        if Path(path).exists():
            return name, path
    return None


def _ass(cues: list[Cue], w: int, h: int, font_name: str) -> str:
    """烧录用的 ASS：画布 = 输出分辨率，字号按分辨率算。白字黑描边，底部居中；竖屏抬高一点，避开平台的操作栏。"""
    vertical = h > w
    size = round(w * 0.072) if vertical else round(h * 0.052)
    outline = max(2, round(size * 0.09))
    margin_v = round(h * (0.17 if vertical else 0.07))
    margin_h = round(w * 0.06)

    def t(x: float) -> str:
        cs = int(round(x * 100))
        return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,{outline},0,2,{margin_h},{margin_h},{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in cues:
        text = c.text.replace("{", "(").replace("}", ")").replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{t(c.start)},{t(c.end)},Default,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def _wrap(text: str, width: float) -> str:
    """按显示宽度（中文 1、其他 0.5，单位：字号）贴着 width 换行，给 drawtext 用（它自己不会换行）。"""
    lines, cur, cw = [], "", 0.0
    for ch in text:
        if ch == "\n":
            lines.append(cur), (cur := ""), (cw := 0.0)
            continue
        wch = 1.0 if ord(ch) >= 0x2E80 else 0.55
        if cw + wch > width and cur:
            lines.append(cur)
            cur, cw = "", 0.0
        cur += ch
        cw += wch
    lines.append(cur)
    return "\n".join(lines)


def _build(plan: Plan, output: Output, burn_override: bool | None, workdir: Path, loudnorm_on: bool) -> _Build:
    kept = output.kept()
    if not kept:
        raise RenderError(f"输出 {output.id!r} 没有保留的片段（全是 cut）。在审阅页把要的片段改回保留，或检查配方的阈值")
    warnings: list[str] = []
    fps = output_fps(plan, output)
    W, H = output_size(plan, output)
    grid = frame_grid(output, fps)
    total_frames = grid[-1][2]
    total = total_frames / float(fps)
    F = f"{fps.numerator}/{fps.denominator}"
    fade = max(0.0, output.audio_fade_ms) / 1000.0

    # ---- 素材探测 ----
    srcs: dict[str, _Src] = {}
    used = {s.source for s in kept} | {b.source for b in output.beds} | {o.source for o in output.overlays if o.kind == "image" and o.source}
    for sid in used:
        s = plan.sources[sid]
        info = _info(plan, sid)
        n_audio = len(info.audio)
        track = s.audio_track or 0
        if n_audio and track >= n_audio:
            raise RenderError(f"sources.{sid}.audio_track={track} 超出范围：{plan.source_path(sid).name} 只有 {n_audio} 条音轨（序号 0..{n_audio - 1}）")
        disp = _display_size(info)
        v = info.video
        sar_fix = False
        if v and v.display_aspect:
            rw, rh = (v.height, v.width) if v.rotation in (90, 270) else (v.width, v.height)
            sar_fix = abs(v.display_aspect * rh / rw - 1) > 0.01
            if v.is_vfr and s.kind == "video":
                warnings.append(f"素材 {sid} 是可变帧率，已统一成恒定 {float(fps):.3f}fps（避免音画错位）")
            if v.rotation:
                log.info("素材 %s 带旋转信息 %d°，按显示方向处理，显示尺寸 %dx%d", sid, v.rotation, *disp)
        srcs[sid] = _Src(plan.source_path(sid), s.kind, info, n_audio, track, disp, sar_fix)
        for w in s.warnings:
            warnings.append(f"素材 {sid}：{w}")

    # ---- 解码块 ----
    chunks: list[_Chunk] = []
    chunk_of: dict[int, _Chunk] = {}
    for k, seg in enumerate(kept):
        if srcs[seg.source].kind == "image":
            continue
        cur = chunks[-1] if chunks else None
        if cur and cur.src == seg.source and seg.start >= cur.last_start - 1e-6 and seg.start - cur.end <= CHUNK_GAP:
            cur.members.append(k)
            cur.end = max(cur.end, seg.end)
            cur.last_start = seg.start
        else:
            cur = _Chunk(seg.source, seg.start, seg.end, seg.start, [k])
            chunks.append(cur)
        chunk_of[k] = cur

    inputs: list[list[str]] = []
    for c in chunks:
        a = ["-ss", _n(c.ss)] if c.ss > 0 else []
        c.idx = len(inputs)
        inputs.append([*a, "-t", _n(c.end - c.ss + 0.3), "-i", str(srcs[c.src].path)])

    V: list[str] = []
    A: list[str] = []

    # 每个块的 split 出口：视频/音频各自按使用者分流
    v_pad: dict[int, str] = {}
    a_pad: dict[int, str] = {}
    for c in chunks:
        s = srcs[c.src]
        has_v = s.info.video is not None and s.kind == "video"
        a_users = [k for k in c.members if not kept[k].mute] if s.n_audio else []
        if has_v:
            if len(c.members) == 1:
                v_pad[c.members[0]] = f"[{c.idx}:v]"
            else:
                labels = "".join(f"[cv{c.idx}_{i}]" for i in range(len(c.members)))
                V.append(f"[{c.idx}:v]split={len(c.members)}{labels}")
                for i, k in enumerate(c.members):
                    v_pad[k] = f"[cv{c.idx}_{i}]"
        if a_users:
            sel = f"[{c.idx}:a:{s.track}]"
            if len(a_users) == 1:
                a_pad[a_users[0]] = sel
            else:
                labels = "".join(f"[ca{c.idx}_{i}]" for i in range(len(a_users)))
                A.append(f"{sel}asplit={len(a_users)}{labels}")
                for i, k in enumerate(a_users):
                    a_pad[k] = f"[ca{c.idx}_{i}]"

    # ---- 逐个片段 ----
    reframe = output.reframe
    kf = [(float(p[0]), float(p[1]), float(p[2])) for p in (reframe.keyframes if reframe else []) if len(p) >= 3]
    mismatch_warned = False
    for k, (seg, f0, f1) in enumerate(grid):
        s = srcs[seg.source]
        n_fr = f1 - f0
        L = n_fr / float(fps)  # 本段输出时长（帧格对齐）
        o_start = f0 / float(fps)
        src_dur = seg.end - seg.start
        content = src_dur / seg.speed if s.kind != "image" else (seg.slot or 0.0)
        # ---- 视频 ----
        if s.kind == "image":
            image_idx = len(inputs)
            motion = seg.evidence.get("motion", "kenburns")
            if motion == "kenburns":
                inputs.append(["-i", str(s.path)])  # 单帧输入，zoompan 展开
                V.append(f"[{image_idx}:v]{_kenburns(W, H, n_fr, fps, k)},setpts=PTS-STARTPTS[v{k}]")
            else:
                inputs.append(["-loop", "1", "-framerate", F, "-t", _n(L + 0.5), "-i", str(s.path)])
                V.append(
                    f"[{image_idx}:v]fps={F}:start_time=0,scale={W}:{H}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=1,"
                    f"trim=end_frame={n_fr},setpts=PTS-STARTPTS[v{k}]"
                )
        elif s.info.video is None or s.kind == "audio":
            V.append(f"color=c=black:s={W}x{H}:r={F},format=yuv420p,setsar=1,trim=end_frame={n_fr},setpts=PTS-STARTPTS[v{k}]")
        else:
            c = chunk_of[k]
            rel = seg.start - c.ss
            ch = [f"trim=start={_n(rel)}:end={_n(rel + src_dur)}", f"setpts=PTS-{_n(rel)}/TB"]
            if seg.reverse:
                if src_dur > 30:
                    warnings.append(f"片段 {seg.id} 倒放 {src_dur:.0f}s：倒放要把整段读进内存，较长的片段可能很慢或爆内存，建议缩短")
                ch += ["reverse", "setpts=PTS-STARTPTS"]
            if abs(seg.speed - 1.0) > 1e-9:
                ch.append(f"setpts=PTS/{_n(seg.speed)}")
            ch.append(f"fps={F}:start_time=0")
            if s.sar_fix:
                ch.append("scale=trunc(iw*sar/2)*2:ih,setsar=1")
            dw, dh = s.disp
            if reframe is not None and dw and dh:
                ao = W / H
                cw, chh = (dh * ao, dh) if dw / dh > ao else (dw, dw / ao)
                cw, chh = int(cw) // 2 * 2, int(chh) // 2 * 2
                if kf:
                    tv = f"(t+{_n(o_start)})"
                    cx = _lerp_expr([(a, b) for a, b, _ in kf], tv)
                    cy = _lerp_expr([(a, c_) for a, _, c_ in kf], tv)
                else:
                    cx = cy = "0.5"
                ch.append(
                    f"crop={cw}:{chh}:x='max(0,min({dw - cw},({cx})*{dw}-{cw / 2:g}))':y='max(0,min({dh - chh},({cy})*{dh}-{chh / 2:g}))'"
                )
                ch.append(f"scale={W}:{H}:flags=bicubic")
            else:
                if dw and dh and abs(dw / dh - W / H) > 0.02 and not mismatch_warned:
                    mismatch_warned = True
                    warnings.append(
                        f"素材画幅 {dw}x{dh} 与输出 {W}x{H} 不一致且没有设置 reframe：画面等比缩小并补黑边。"
                        f"要铺满就在方案里给输出加 reframe（mode=center 居中裁切）"
                    )
                ch.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic")
                ch.append(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
            pad_s = max(1.0, L - content + 1.0)  # slot 比内容长时定格最后一帧补齐
            ch += ["setsar=1", "format=yuv420p", f"tpad=stop_mode=clone:stop_duration={_n(pad_s)}", f"trim=end_frame={n_fr}", "setpts=PTS-STARTPTS"]
            V.append(f"{v_pad[k]}{','.join(ch)}[v{k}]")

        # ---- 音频 ----
        ns = int(round(L * AUDIO_SR))
        has_audio = (not seg.mute) and k in a_pad
        if not has_audio:
            if not seg.mute and s.kind != "image":
                warnings.append(f"素材 {seg.source} 没有音频流，片段 {seg.id} 用静音代替")
            A.append(f"anullsrc=r={AUDIO_SR}:cl=stereo:d={_n(L + 0.1)},apad=whole_len={ns},atrim=end_sample={ns},asetpts=PTS-STARTPTS[a{k}]")
            continue
        c = chunk_of[k]
        rel = seg.start - c.ss
        ch = [f"atrim=start={_n(rel)}:end={_n(rel + src_dur)}", "asetpts=PTS-STARTPTS",
              f"aresample={AUDIO_SR}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if seg.reverse:
            ch.append("areverse")
        if abs(seg.speed - 1.0) > 1e-9:
            ch.append(_atempo(seg.speed))
        if abs(seg.pitch) > 1e-9:
            ch.append(f"rubberband=pitch={2 ** (seg.pitch / 12):.6f}")
        ch += [f"apad=whole_len={ns}", f"atrim=end_sample={ns}"]
        f_s = min(fade, L / 2)
        prev = grid[k - 1][0] if k > 0 else None
        nxt = grid[k + 1][0] if k + 1 < len(grid) else None

        def continuous(a: Segment, b: Segment) -> bool:  # a 紧接着 b，素材里首尾相接、没变速没倒放
            return (a.source == b.source and abs(a.end - b.start) < 1e-3 and a.speed == b.speed == 1.0
                    and not a.reverse and not b.reverse and a.slot is None and b.slot is None and not a.mute and not b.mute)

        if f_s > 0:
            if prev is None or not continuous(prev, seg):
                ch.append(f"afade=t=in:st=0:d={_n(f_s)}")
            if nxt is None or not continuous(seg, nxt):
                end_t = min(L, content)  # 内容比片段短（slot 补静音）时，在内容结束处淡出
                ch.append(f"afade=t=out:st={_n(max(0.0, end_t - f_s))}:d={_n(f_s)}")
        ch.append("asetpts=PTS-STARTPTS")
        A.append(f"{a_pad[k]}{','.join(ch)}[a{k}]")

    V.append("".join(f"[v{k}]" for k in range(len(grid))) + f"concat=n={len(grid)}:v=1:a=0[vcat]")
    A.append("".join(f"[a{k}]" for k in range(len(grid))) + f"concat=n={len(grid)}:v=0:a=1[acat]")

    # ---- 叠加层（图片、文字）----
    vlabel = "vcat"
    files: dict[str, str] = {}
    font = _font()
    for j, ov in enumerate(output.overlays):
        nxt_label = f"vo{j}"
        at, dur = ov.at, ov.duration
        if ov.at >= total or dur <= 0:
            warnings.append(f"叠加层 {j}（{ov.kind}）的起点 {ov.at:.2f}s 超出输出时长 {total:.2f}s，已忽略")
            continue
        en = f"enable='between(t,{_n(at)},{_n(at + dur)})'"
        if ov.kind == "text":
            if font is None:
                warnings.append("找不到中文字体，文字叠加层用 ffmpeg 默认字体，中文可能显示成方块")
            fs = round(H * (0.055 if H >= W else 0.045))
            fname = f"overlay{j}.txt"
            files[fname] = _wrap(ov.text, max(4.0, W * 0.88 / fs))
            y = {"top": "h*0.10", "bottom": "h*0.70"}.get(ov.position, "(h-text_h)/2")
            ff = f"fontfile={_q(font[1])}:" if font else ""
            V.append(
                f"[{vlabel}]drawtext={ff}textfile={_q(workdir / fname)}:expansion=none:fontsize={fs}:fontcolor=white:"
                f"borderw={max(2, fs // 14)}:bordercolor=black:box=1:boxcolor=black@0.35:boxborderw={fs // 4}:"
                f"x=(w-text_w)/2:y={y}:{en}[{nxt_label}]"
            )
        elif ov.kind == "image" and ov.source:
            s = srcs[ov.source]
            n_o = max(2, round(dur * float(fps)))
            full = ov.position in ("full",) or ov.motion == "kenburns"
            bw, bh = (W, H) if ov.position == "full" else (
                (int(W * 0.9) // 2 * 2, int(H * 0.30) // 2 * 2) if ov.position in ("top", "bottom") else (int(W * 0.8) // 2 * 2, int(H * 0.8) // 2 * 2))
            idx = len(inputs)
            if ov.motion == "kenburns":
                inputs.append(["-i", str(s.path)])
                cover = f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh}"
                z = f"1+{_ZOOM}*on/{n_o}"
                chain = (f"{cover},scale={2 * bw}:{2 * bh},zoompan=z='{z}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d={n_o}:s={bw}x{bh}:fps={F},"
                         f"setsar=1,format=yuva420p")
            else:
                inputs.append(["-loop", "1", "-framerate", F, "-t", _n(dur + 0.2), "-i", str(s.path)])
                chain = (f"scale={bw}:{bh}:force_original_aspect_ratio={'increase' if full else 'decrease'}"
                         + (f",crop={bw}:{bh}" if full else "") + ",setsar=1,format=yuva420p")
            x = "(W-w)/2"
            y = {"top": "H*0.08", "bottom": "H*0.62"}.get(ov.position, "(H-h)/2")
            V.append(f"[{idx}:v]{chain},setpts=PTS-STARTPTS+{_n(at)}/TB[ovs{j}]")
            V.append(f"[{vlabel}][ovs{j}]overlay=x={x}:y={y}:{en}:eof_action=pass[{nxt_label}]")
        else:
            warnings.append(f"叠加层 {j} 类型 {ov.kind!r} 无法处理（图片叠加层需要 source），已忽略")
            continue
        vlabel = nxt_label

    # ---- 字幕 ----
    caps = output.captions
    burn = caps.burn if burn_override is None else burn_override
    cues: list[Cue] = []
    if caps.enabled or burn_override is not None and burn_override:
        cues = cues_for(plan, output)
        if not cues:
            warnings.append("字幕已开启但没有可用的文字稿（素材没有逐词文字稿，也没有可用的语音识别模型），没有生成字幕")
    if burn and cues:
        name = font[0] if font else "Arial"
        if font is None:
            warnings.append("找不到中文字体，烧录字幕用默认字体，中文可能显示成方块")
        files["captions.ass"] = _ass(cues, W, H, name)
        fonts_dir = workdir / "fonts"
        fonts_arg = f":fontsdir={_q(fonts_dir)}" if font else ""
        V.append(f"[{vlabel}]ass=filename={_q(workdir / 'captions.ass')}{fonts_arg}[vsub]")
        vlabel = "vsub"
    elif burn and not cues:
        burn = False
    V.append(f"[{vlabel}]format=yuv420p[vout]")

    # ---- 背景音与配音 ----
    ns_total = int(round(total * AUDIO_SR))
    alabel = "acat"
    beds: list[tuple[int, Bed]] = []
    for j, b in enumerate(output.beds):
        if b.at >= total:
            warnings.append(f"bed {j}（{b.source}）的起点 {b.at:.2f}s 超出输出时长 {total:.2f}s，已忽略")
        elif not srcs[b.source].n_audio:
            warnings.append(f"bed {j} 的素材 {b.source} 没有音频流，已忽略")
        else:
            beds.append((j, b))
    n_duck = sum(1 for _, b in beds if b.duck_db > 0)
    if n_duck:  # 每条要闪避的 bed 各用一份主轨音频当侧链
        A.append("[acat]asplit=%d[amain]%s" % (n_duck + 1, "".join(f"[sc{i}]" for i in range(n_duck))))
        alabel = "amain"
    mix_in, sc_i = [f"[{alabel}]"], 0
    for j, b in beds:
        s = srcs[b.source]
        length = (b.end - b.start) if b.end is not None else None
        read = min(length if length is not None else 1e9, total - b.at) + 0.3
        inputs.append([*(["-ss", _n(b.start)] if b.start > 0 else []), "-t", _n(read), "-i", str(s.path)])
        out_end = min(b.at + length, total) if length is not None else total
        ch = ["asetpts=PTS-STARTPTS", f"aresample={AUDIO_SR}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if length is not None:
            ch.append(f"atrim=end={_n(length)}")
        if b.gain_db:
            ch.append(f"volume={_n(b.gain_db)}dB")
        ch.append("afade=t=in:st=0:d=0.02")  # 起点淡入 20ms，防爆音
        if b.at > 0:
            ch.append(f"adelay=delays={int(round(b.at * 1000))}:all=1")
        ch += [f"apad=whole_dur={_n(total)}", f"atrim=end={_n(total)}"]
        if b.role == "music":  # BGM 收尾淡出，别突然断掉
            fo = min(1.0, 0.12 * max(out_end - b.at, 0.1))
            ch.append(f"afade=t=out:st={_n(max(0.0, out_end - fo))}:d={_n(fo)}")
        lab = f"bed{j}"
        A.append(f"[{len(inputs) - 1}:a:{s.track}]{','.join(ch)}[{lab}]")
        if b.duck_db > 0:
            # 压低量 ≈ (1-1/ratio)·(人声电平-阈值)；按典型语音电平高出阈值 21 dB 估算 ratio，实际压多少随人声响度略有出入
            ratio = min(20.0, 1.0 / (1.0 - min(max(b.duck_db / 21.0, 0.1), 0.92)))
            A.append(f"[{lab}][sc{sc_i}]sidechaincompress=threshold=0.0056:ratio={ratio:.2f}:attack=15:release=350:makeup=1[{lab}d]")
            lab += "d"
            sc_i += 1
        mix_in.append(f"[{lab}]")
    if beds:
        A.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=first:normalize=0:dropout_transition=0[amix]")
        alabel = "amix"
        if not loudnorm_on:
            A.append("[amix]alimiter=limit=0.97:level=0[amixl]")
            alabel = "amixl"
    A.append(f"[{alabel}]apad=whole_len={ns_total},atrim=end_sample={ns_total},asetpts=PTS-STARTPTS[apre]")
    return _Build(inputs, V, A, (W, H), fps, total_frames, total, files, warnings, cues, burn)


# ================================================================== 执行


def _which_fonts_dir(workdir: Path) -> None:
    f = _font()
    if f:
        d = workdir / "fonts"
        d.mkdir(exist_ok=True)
        link = d / Path(f[1]).name
        if not link.exists():
            link.symlink_to(f[1])


_PREFIX = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats"]


def _ffmpeg(args: list[str], *, progress: Callable[[float], None] | None = None, total: float = 0.0, info: bool = False) -> str:
    """跑 ffmpeg，返回 stderr 文本；失败抛 RenderError（带 stderr 末尾几行）。progress 解析 -progress pipe:1。"""
    cmd = [*_PREFIX, "-loglevel", "info" if info else "error", *args]
    t0 = time.perf_counter()
    err = tempfile.TemporaryFile("w+b")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE if progress else subprocess.DEVNULL, stderr=err, text=True)
    except FileNotFoundError as e:
        raise RenderError("找不到 ffmpeg：请安装 ffmpeg（macOS: brew install ffmpeg）并确认它在 PATH 里") from e
    if progress and p.stdout:
        for line in p.stdout:
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / max(total, 1e-6)))
                except ValueError:
                    pass
    rc = p.wait()
    err.seek(0)
    text = err.read().decode("utf-8", "replace")
    err.close()
    log.debug("ffmpeg 退出码 %d，%.2fs", rc, time.perf_counter() - t0)
    if rc != 0:
        tail = "\n".join(text.strip().splitlines()[-6:])
        raise RenderError(f"ffmpeg 失败（退出码 {rc}）：\n{tail}\n命令：{' '.join(cmd[:12])} ...（完整滤镜图见 RenderResult.filter_graph 或用 dry_run=True）")
    return text


def _probe_result(path: Path) -> tuple[float, float | None, float | None]:
    """(容器时长, 视频流时长, 音频流时长)。流时长缺失时退回容器时长。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RenderError(f"成品 {path} 读不了：{r.stderr.strip()[-200:]}")
    d = json.loads(r.stdout)
    fmt = float(d["format"].get("duration") or 0.0)
    out: dict[str, float | None] = {"video": None, "audio": None}
    for s in d.get("streams", []):
        k = s.get("codec_type")
        if k in out and out[k] is None:
            out[k] = float(s["duration"]) if s.get("duration") not in (None, "N/A") else fmt
    return fmt, out["video"], out["audio"]


def _measure_loudness(inputs: list[list[str]], graph_a: str, workdir: Path, total: float, target: float) -> dict | None:
    """第一遍：只跑音频图，让 loudnorm 打印测量值（JSON）。静音素材返回 None。"""
    (workdir / "graph_a.txt").write_text(graph_a)
    args = [x for inp in inputs for x in inp]
    text = _ffmpeg(
        [*args, "-/filter_complex", str(workdir / "graph_a.txt"), "-map", "[am]", "-f", "null", "-"],
        info=True,
    )
    m = list(re.finditer(r"\{[^{}]*\"input_i\"[^{}]*\}", text))
    if not m:
        raise RenderError("loudnorm 测量没有输出结果（ffmpeg 版本不支持 loudnorm 的 print_format=json？）")
    d = json.loads(m[-1].group(0))
    if d["input_i"] in ("-inf", "inf") or float(d["input_i"]) < -69:
        return None
    return {k: d[k] for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")} | {"target": target}


def render(
    plan: Plan | str | Path,
    *,
    output_ids: list[str] | None = None,
    out_dir: str | Path | None = None,
    burn_captions: bool | None = None,
    loudnorm: bool | float | None = None,
    progress: ProgressFn | None = None,
    dry_run: bool = False,
) -> list[RenderResult]:
    """把方案的输出渲染成 mp4。

    output_ids：None = 所有 selected=True 的输出。out_dir：None = 方案旁边。
    burn_captions：None = 听方案的；True/False 覆盖 captions.burn。
    loudnorm：None = 竖屏输出默认 -14 LUFS，其他不做；True = -14；数字 = 目标 LUFS；False = 不做。
    progress(stage, fraction)：stage 取 prepare / measure / encode / verify / done。dry_run：只构造命令，不写成品文件。
    """
    if not isinstance(plan, Plan):
        plan = Plan.load(plan)
    outs = [plan.output(i) for i in output_ids] if output_ids else [o for o in plan.outputs if o.selected]
    if not outs:
        raise RenderError("没有要渲染的输出：方案里没有 selected=True 的输出，也没有传 output_ids")
    base = Path(out_dir) if out_dir else (plan.path.parent if plan.path else Path.cwd())
    stem = default_stem(plan)
    results = []
    for oi, o in enumerate(outs):
        def cb(stage: str, frac: float, _oi: int = oi) -> None:
            if progress:
                progress(stage, frac)

        results.append(_render_one(plan, o, base, stem, burn_captions, loudnorm, cb, dry_run))
    return results


def _render_one(plan: Plan, o: Output, base: Path, stem: str, burn_override: bool | None,
                loudnorm: bool | float | None, cb: ProgressFn, dry_run: bool) -> RenderResult:
    t0 = time.perf_counter()
    cb("prepare", 0.0)
    out_path = base / f"{stem}.{o.id}.mp4"
    srt_path = base / f"{stem}.{o.id}.srt"
    W, H = output_size(plan, o)
    if loudnorm is None:
        target: float | None = LOUDNESS_TARGET if H > W else None
    elif loudnorm is True:
        target = LOUDNESS_TARGET
    elif loudnorm is False:
        target = None
    else:
        target = float(loudnorm)
    workdir = Path(tempfile.mkdtemp(prefix="autocut-render-"))
    _which_fonts_dir(workdir)
    keep_workdir = dry_run
    try:
        b = _build(plan, o, burn_override, workdir, target is not None)
        for name, content in b.files.items():
            (workdir / name).write_text(content, encoding="utf-8")
        warnings = list(b.warnings)
        graph_v = ";\n".join(b.video)
        graph_a = ";\n".join(b.audio)

        # ---- 响度 ----
        lufs: dict | None = None
        a_tail = ""
        if target is not None:
            if dry_run:
                a_tail = f";\n[apre]loudnorm=I={target}:TP={TRUE_PEAK}:LRA=11,aresample={AUDIO_SR}[aout]"
                warnings.append("dry_run 没有预先测量响度：实际渲染会先测一遍再做线性 loudnorm")
            else:
                cb("measure", 0.0)
                meas = _measure_loudness(
                    b.inputs, graph_a + f";\n[apre]loudnorm=I={target}:TP={TRUE_PEAK}:LRA=11:print_format=json[am]", workdir, b.total, target)
                if meas is None:
                    warnings.append("素材整体是静音，跳过响度标准化")
                    a_tail = ";\n[apre]anull[aout]"
                else:
                    lufs = meas
                    a_tail = (
                        f";\n[apre]loudnorm=I={target}:TP={TRUE_PEAK}:LRA=11:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
                        f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true,"
                        f"aresample={AUDIO_SR},apad=whole_len={int(round(b.total * AUDIO_SR))},atrim=end_sample={int(round(b.total * AUDIO_SR))}[aout]"
                    )
                    log.info("响度 %s：测得 %.1f LUFS / %.1f dBTP → 目标 %.1f LUFS", o.id, float(meas["input_i"]), float(meas["input_tp"]), target)
        else:
            a_tail = ";\n[apre]anull[aout]"
        graph = graph_v + ";\n" + graph_a + a_tail + "\n"
        gpath = workdir / "graph.txt"
        gpath.write_text(graph)

        # ---- 编码 ----
        part = base / f".{out_path.stem}.part.mp4"
        F = f"{b.fps.numerator}/{b.fps.denominator}"
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high"] if VIDEO_ENCODER == "libx264" else [
            "-c:v", VIDEO_ENCODER, "-b:v", f"{max(2, round(W * H * float(b.fps) * 0.1 / 1e6))}M"]
        meta = f"autocut render; output={o.id}; captions={'burned' if b.burn else ('sidecar' if b.cues else 'none')}"
        args = [
            "-y", *[x for inp in b.inputs for x in inp],
            "-/filter_complex", str(gpath), "-map", "[vout]", "-map", "[aout]",
            *enc, "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", F,
            "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_SR), "-ac", "2",
            "-movflags", "+faststart", "-metadata", f"comment={meta}",
            "-t", f"{b.total:.6f}", "-progress", "pipe:1", str(part),
        ]
        cmd = [*_PREFIX, "-loglevel", "error", *args]
        log.info("渲染 %s：%dx%d @%.3ffps，%d 帧（%.2fs），%d 个片段，输入 %d 个", o.id, W, H, float(b.fps), b.total_frames, b.total, len(o.kept()), len(b.inputs))
        if dry_run:
            log.info("dry_run：滤镜图脚本在 %s", gpath)
            cmd[cmd.index(str(part))] = str(out_path)
            return RenderResult(o.id, out_path, o.duration, None, None, srt_path if b.cues else None, cmd, warnings, graph, lufs)

        base.mkdir(parents=True, exist_ok=True)
        if b.cues or (o.captions.enabled and b.cues):
            srt_path.write_text(format_srt(b.cues), encoding="utf-8")
        cb("encode", 0.0)
        try:
            _ffmpeg(args, progress=lambda f: cb("encode", f), total=b.total)
            os.replace(part, out_path)
        finally:
            if part.exists():
                part.unlink()

        # ---- 实测 ----
        cb("verify", 0.0)
        dur, vd, ad = _probe_result(out_path)
        drift = abs(vd - ad) if vd is not None and ad is not None else None
        frame = 1.0 / float(b.fps)
        expected = o.duration
        if abs(dur - expected) > frame + 1e-3:
            warnings.append(f"成品时长 {dur:.3f}s 与方案 {expected:.3f}s 相差 {abs(dur - expected):.3f}s（{abs(dur - expected) / frame:.1f} 帧）")
        if drift is not None and drift > frame + 1e-3:
            warnings.append(f"音画时长差 {drift:.3f}s（{drift / frame:.1f} 帧）：视频 {vd:.3f}s / 音频 {ad:.3f}s")
        for w in warnings:
            log.warning("渲染 %s：%s", o.id, w)
        cb("done", 1.0)
        log.info("渲染 %s 完成：%s（%.2fs，用时 %.1fs，约 %.1fx 实时）", o.id, out_path, dur, time.perf_counter() - t0, dur / max(time.perf_counter() - t0, 1e-6))
        return RenderResult(o.id, out_path, expected, dur, drift, srt_path if b.cues else None, cmd, warnings, graph, lufs)
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

