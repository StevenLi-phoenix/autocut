"""导出：把方案的一个输出写成剪辑软件能接着改的工程（C1/C2：交接最容易坏的地方都在这里设防）。

- fcpxml  FCPXML 1.10（Final Cut Pro、DaVinci Resolve）。asset-clip 把音视频绑在一起（联动，S15）；asset 用绝对路径的
          file:// URL（S33）；时间是以帧为单位的有理数（S18/S19：不会因为奇怪的帧率累积误差）；竖屏尺寸来自输出设置（S10）；
          多音轨素材用 audio-channel-source 指明输出用哪一条（S55/S56）。被剪掉的片段可以写成标记或禁用片段，剪辑软件里还能找回。
- xmeml   FCP7 XML（Premiere）。音视频 clipitem 用 link 互相关联。
- edl     CMX3600。
- srt     输出时间轴的字幕。
- markers FCPXML 标记：卡点、高光、音节和音乐的节拍网格（S70：在剪辑软件里接着编排）。
时间线用输出的帧率（跟渲染一致，frame_grid 取整，总误差 ≤ 半帧）。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

from autocut.captions import cues_for, write_srt
from autocut.plan import REASONS, Bed, Output, Plan, Segment
from autocut.probe import MediaInfo, probe
from autocut.render import _display_size, default_stem, frame_grid, output_fps, output_size

log = logging.getLogger(__name__)

FORMATS = ("fcpxml", "xmeml", "edl", "srt", "markers")
_EXT = {"fcpxml": ".fcpxml", "xmeml": ".xml", "edl": ".edl", "srt": ".srt", "markers": ".markers.fcpxml"}
CUT_MODES = ("markers", "disabled", "none")
# 这些理由的片段起点会作为"标记"导出（卡点/高光/音节/镜头）
_MARK_REASONS = ("beat", "syllable", "highlight", "shot", "lead_in", "action")


@dataclass
class _Asset:
    sid: str
    path: Path
    kind: str
    info: MediaInfo
    disp: tuple[int, int]
    fps: Fraction
    track: int
    first_ch: int  # 输出用的音轨在整个文件所有声道里的起始序号（1 起）
    ref: str = ""
    fmt_ref: str = ""


@dataclass
class _Clip:
    seg: Segment
    asset: _Asset
    f0: int  # 时间线起止帧
    f1: int
    in_f: int  # 素材入点（时间线帧率下的帧数）


def _fps_parts(fps: Fraction) -> tuple[int, bool]:
    """xmeml 的 (timebase, ntsc)：29.97 → (30, True)。"""
    if fps.denominator == 1001:
        return round(fps.numerator / 1001), True
    return round(float(fps)), False


def _prepare(plan: Plan, output: Output):
    fps = output_fps(plan, output)
    W, H = output_size(plan, output)
    grid = frame_grid(output, fps)
    assets: dict[str, _Asset] = {}

    def asset(sid: str) -> _Asset:
        if sid not in assets:
            s = plan.sources[sid]
            p = plan.source_path(sid).resolve()
            if not p.exists():
                raise FileNotFoundError(f"素材文件不存在：{p}（sources.{sid}.path={s.path!r}）。导出的工程会引用这个路径，先修正方案里的路径")
            info = probe(p)
            track = s.audio_track or 0
            first_ch = 1 + sum(a.channels for a in info.audio[:track])
            vf = info.video.fps if info.video and info.video.fps > 0 else float(fps)
            from autocut.render import snap_fps

            assets[sid] = _Asset(sid, p, s.kind, info, _display_size(info), snap_fps(vf), track, first_ch)
        return assets[sid]

    clips = [_Clip(seg, asset(seg.source), f0, f1, int(round(seg.start * float(fps)))) for seg, f0, f1 in grid]
    beds = [(b, asset(b.source)) for b in output.beds if plan.sources[b.source].kind != "image"]
    return fps, (W, H), clips, beds, assets


def _out_path(plan: Plan, output: Output, fmt: str, out: str | Path | None) -> Path:
    if out is not None:
        return Path(out)
    base = plan.path.parent if plan.path else Path.cwd()
    return base / f"{default_stem(plan)}.{output.id}{_EXT[fmt]}"


def _write_xml(root: ET.Element, path: Path, doctype: str) -> None:
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE {doctype}>\n{body}\n', encoding="utf-8")


def export(
    plan: Plan | str | Path,
    fmt: str,
    out: str | Path | None = None,
    output_id: str | None = None,
    *,
    cuts: str = "markers",
) -> Path:
    """导出方案的一个输出（默认第一个），返回写出的文件路径。

    fmt：fcpxml | xmeml | edl | srt | markers。
    cuts（只对 fcpxml 有效）：被剪掉的片段怎么留给剪辑软件 —— markers（默认，在剪口处打标记，写明原时间和理由）、
    disabled（作为禁用的连接片段，在剪辑软件里一键启用）、none。
    """
    if not isinstance(plan, Plan):
        plan = Plan.load(plan)
    fmt = {"xml": "xmeml", "fcp7": "xmeml", "cmx3600": "edl"}.get(fmt, fmt)
    if fmt not in FORMATS:
        raise ValueError(f"不支持的导出格式 {fmt!r}；可选：{list(FORMATS)}")
    if cuts not in CUT_MODES:
        raise ValueError(f"cuts 只能是 {list(CUT_MODES)}，收到 {cuts!r}")
    output = plan.output(output_id)
    path = _out_path(plan, output, fmt, out)
    if fmt == "srt":
        cues = cues_for(plan, output)
        if not cues:
            raise ValueError(
                f"输出 {output.id!r} 没有可用的文字稿，导出不了字幕：给素材生成逐词文字稿（sources.<id>.words），或安装语音识别模型"
            )
        write_srt(cues, path)
    elif fmt == "edl":
        _write_edl(plan, output, path)
    elif fmt == "xmeml":
        _write_xmeml(plan, output, path)
    else:
        _write_fcpxml(plan, output, path, markers_only=(fmt == "markers"), cuts=cuts)
    log.info("导出 %s：%s → %s", fmt, output.id, path)
    return path


# ================================================================== FCPXML


class _T:
    """时间线的有理数时间：帧数 → "1001/30000s"。"""

    def __init__(self, fps: Fraction) -> None:
        self.fd = 1 / fps  # 一帧的秒数

    def __call__(self, frames: int | Fraction) -> str:
        t = Fraction(frames) * self.fd
        return "0s" if t == 0 else (f"{t.numerator}s" if t.denominator == 1 else f"{t.numerator}/{t.denominator}s")


def _marker_points(plan: Plan, output: Output, clips: list[_Clip], include_cuts: bool, include_uncertain: bool, grid_beats: bool,
                   fps: Fraction) -> list[tuple[int, str, str, bool]]:
    """(时间线帧, 名称, 备注, 待办)。"""
    pts: list[tuple[int, str, str, bool]] = []
    fl = float(fps)
    # 保留片段：卡点/高光等理由，或拿不准
    for c in clips:
        s = c.seg
        if s.reason in _MARK_REASONS:
            label = REASONS.get(s.reason, s.reason)
            if s.text:
                label += f"：{s.text[:20]}"
            pts.append((c.f0, label, s.detail, False))
        if include_uncertain and s.uncertain:
            pts.append((c.f0, f"拿不准 {s.confidence:.2f}", s.detail or REASONS.get(s.reason, s.reason), True))
    # 被剪掉的片段：标在剪口上（后一个保留片段的起点；没有就标在末尾）
    if include_cuts:
        ki = 0
        for s in output.segments:
            if s.action == "keep":
                ki += 1
                continue
            f = clips[ki].f0 if ki < len(clips) else (clips[-1].f1 - 1 if clips else 0)
            name = f"剪掉 {REASONS.get(s.reason, s.reason)} {s.src_duration:.2f}s"
            where = f"素材 {s.source} {s.start:.2f}–{s.end:.2f}s"
            note = f"{s.detail}（{where}）" if s.detail else where
            pts.append((f, name, note, False))
    # 音乐的节拍网格
    if grid_beats:
        total_f = clips[-1].f1 if clips else 0
        for b in output.beds:
            if b.role != "music" or plan.sources[b.source].kind == "image":
                continue
            try:
                from autocut.analysis import Media

                br = Media(plan.source_path(b.source)).beats()
            except Exception as e:  # noqa: BLE001
                log.warning("取节拍失败（%s）：%s", b.source, e)
                continue
            end = b.end if b.end is not None else float("inf")
            downs = {round(x, 3) for x in br.downbeats}
            for i, t_src in enumerate(br.beats):
                if t_src < b.start - 1e-6 or t_src > end:
                    continue
                f = int(round((b.at + t_src - b.start) * fl))
                if f >= total_f:
                    continue
                bar = round(t_src, 3) in downs
                pts.append((f, f"{'小节' if bar else '拍'} {i + 1}", f"{br.bpm:.0f} BPM", False))
    return sorted(pts)


def _write_fcpxml(plan: Plan, output: Output, path: Path, *, markers_only: bool, cuts: str) -> None:
    fps, (W, H), clips, beds, assets = _prepare(plan, output)
    T = _T(fps)
    total_f = clips[-1].f1
    root = ET.Element("fcpxml", version="1.10")
    res = ET.SubElement(root, "resources")
    ET.SubElement(res, "format", id="r1", frameDuration=T(1), width=str(W), height=str(H), colorSpace="1-1-1 (Rec. 709)")
    n = 1
    fmt_ids: dict[tuple, str] = {((W, H), fps): "r1"}  # 和时间线同尺寸同帧率的素材直接用 r1
    for a in assets.values():
        n += 1
        a.ref = f"r{n}"
        if a.kind == "image" or a.info.video is not None:
            key = (a.disp, a.fps if a.kind != "image" else None)
            if key not in fmt_ids:
                n += 1
                fmt_ids[key] = f"r{n}"
                f = ET.SubElement(res, "format", id=fmt_ids[key], width=str(a.disp[0]), height=str(a.disp[1]))
                if a.kind == "image":
                    f.set("name", "FFVideoFormatRateUndefined")
                else:
                    f.set("frameDuration", _T(a.fps)(1))
                    f.set("colorSpace", "1-1-1 (Rec. 709)")
            a.fmt_ref = fmt_ids[key]
    for a in assets.values():
        dur_frames = int(a.info.duration * float(a.fps)) if a.kind != "image" else 0
        el = ET.SubElement(res, "asset", id=a.ref, name=a.path.stem, start="0s", duration=_T(a.fps)(dur_frames))
        if a.fmt_ref:
            el.set("format", a.fmt_ref)
            el.set("hasVideo", "1")
            el.set("videoSources", "1")
        if a.info.audio:
            el.set("hasAudio", "1")
            el.set("audioSources", str(len(a.info.audio)))
            el.set("audioChannels", str(sum(x.channels for x in a.info.audio)))
            el.set("audioRate", str(a.info.audio[0].sample_rate))
        ET.SubElement(el, "media-rep", kind="original-media", src=a.path.as_uri())

    lib = ET.SubElement(root, "library")
    ev = ET.SubElement(lib, "event", name=default_stem(plan))
    proj = ET.SubElement(ev, "project", name=f"{default_stem(plan)} · {output.id}")
    seq = ET.SubElement(proj, "sequence", format="r1", duration=T(total_f), tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")

    els: list[ET.Element] = []
    for c in clips:
        a = c.asset
        name = a.path.stem if not c.seg.text else f"{c.seg.text[:24]}"
        if a.kind == "image":
            el = ET.SubElement(spine, "video", ref=a.ref, offset=T(c.f0), name=a.path.stem, start="0s", duration=T(c.f1 - c.f0))
        else:
            el = ET.SubElement(spine, "asset-clip", ref=a.ref, offset=T(c.f0), name=name, start=T(c.in_f), duration=T(c.f1 - c.f0),
                               format=a.fmt_ref or "r1", tcFormat="NDF")
            if a.info.audio:
                el.set("audioRole", "dialogue")
        els.append(el)
        s = c.seg
        if (abs(s.speed - 1.0) > 1e-9 or s.reverse) and a.kind != "image":
            # 变速/倒放：timeMap 把片段时间映射回素材时间（best-effort，导入后请在剪辑软件里核对速度）
            src_len = int(round((s.end - s.start) * float(fps)))
            tm = ET.SubElement(el, "timeMap")
            v0, v1 = (T(src_len), "0s") if s.reverse else ("0s", T(src_len))
            ET.SubElement(tm, "timept", time="0s", value=v0, interp="linear")
            ET.SubElement(tm, "timept", time=T(c.f1 - c.f0), value=v1, interp="linear")
        if s.mute and a.info.audio:
            ET.SubElement(el, "adjust-volume", amount="-96dB")
        if len(a.info.audio) > 1 and a.kind != "image":  # 多音轨：只启用方案选定的那一条
            for i, at in enumerate(a.info.audio):
                first = 1 + sum(x.channels for x in a.info.audio[:i])
                src_ch = ", ".join(str(first + k) for k in range(at.channels))
                ET.SubElement(el, "audio-channel-source", srcCh=src_ch, role=f"dialogue.{at.title or f'track-{i + 1}'}",
                              active="1" if i == a.track else "0")

    def anchor(frame: int) -> tuple[ET.Element, _Clip, int]:
        """时间线上 frame 所在的主轨片段，以及相对该片段起点的偏移帧数。"""
        for el, c in zip(els, clips):
            if c.f0 <= frame < c.f1:
                return el, c, frame - c.f0
        return els[-1], clips[-1], clips[-1].f1 - clips[-1].f0 - 1

    # 标记
    pts = _marker_points(plan, output, clips, include_cuts=(cuts == "markers" and not markers_only), include_uncertain=not markers_only,
                         grid_beats=markers_only, fps=fps)
    for f, name, note, todo in pts:
        el, c, off = anchor(f)
        m = ET.SubElement(el, "marker", start=T(c.in_f + off), duration=T(1), value=name)
        if note:
            m.set("note", note)
        if todo:
            m.set("completed", "0")

    # 被剪掉的片段作为禁用的连接片段（在剪辑软件里一键启用就能找回）
    if cuts == "disabled" and not markers_only:
        ki = 0
        for s in output.segments:
            if s.action == "keep":
                ki += 1
                continue
            if plan.sources[s.source].kind == "image":
                continue
            a = assets.get(s.source) or None
            if a is None:
                continue
            host = min(ki, len(clips) - 1)
            c = clips[host]
            frame = c.f0 if ki < len(clips) else c.f1 - 1
            el, hc, off = anchor(frame)
            dur = max(1, int(round(s.src_duration * float(fps))))
            ET.SubElement(el, "asset-clip", ref=a.ref, lane="1", offset=T(hc.in_f + off), name=f"[剪掉] {REASONS.get(s.reason, s.reason)}",
                          start=T(int(round(s.start * float(fps)))), duration=T(dur), format=a.fmt_ref or "r1", enabled="0")

    # 背景音/配音：作为连接在主轨上的音频（ducking 不导出，在剪辑软件里用自动闪避或手动压）
    if not markers_only:
        for b, a in beds:
            if not a.info.audio:
                continue
            f_at = int(round(b.at * float(fps)))
            if f_at >= total_f:
                continue
            length = (b.end - b.start) if b.end is not None else a.info.duration - b.start
            dur = min(int(round(length * float(fps))), total_f - f_at)
            el, hc, off = anchor(f_at)
            bed_el = ET.SubElement(el, "asset-clip", ref=a.ref, lane="-1", offset=T(hc.in_f + off), name=a.path.stem,
                                   start=T(int(round(b.start * float(fps)))), duration=T(dur), audioRole="music" if b.role == "music" else "dialogue")
            if b.gain_db:
                ET.SubElement(bed_el, "adjust-volume", amount=f"{b.gain_db:g}dB")
            if b.duck_db:
                log.info("bed %s 的 ducking（%.0fdB）不随 FCPXML 导出：在剪辑软件里用自动闪避或手动压低", a.sid, b.duck_db)
    if output.overlays:
        log.warning("%d 个叠加层（图片/文字）不随 FCPXML 导出，需要在剪辑软件里重做", len(output.overlays))

    # 元素顺序要符合 DTD：timeMap → adjust-* → audio-channel-source → 连接片段 → marker
    order = {"timeMap": 0, "adjust-volume": 1, "audio-channel-source": 2, "asset-clip": 3, "marker": 4}
    for el in els:
        kids = list(el)
        kids.sort(key=lambda k: order.get(k.tag, 9))
        for k in list(el):
            el.remove(k)
        for k in kids:
            el.append(k)
    _write_xml(root, path, "fcpxml")


# ================================================================== xmeml（FCP7 XML）


def _rate(parent: ET.Element, fps: Fraction) -> None:
    tb, ntsc = _fps_parts(fps)
    r = ET.SubElement(parent, "rate")
    ET.SubElement(r, "timebase").text = str(tb)
    ET.SubElement(r, "ntsc").text = "TRUE" if ntsc else "FALSE"


def _sub(parent: ET.Element, tag: str, text: str | int | None = None) -> ET.Element:
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = str(text)
    return e


def _pathurl(p: Path) -> str:
    return "file://localhost" + quote(str(p), safe="/")


def _write_xmeml(plan: Plan, output: Output, path: Path) -> None:
    fps, (W, H), clips, beds, assets = _prepare(plan, output)
    total_f = clips[-1].f1
    root = ET.Element("xmeml", version="4")
    seq = _sub(root, "sequence", None)
    seq.set("id", "sequence-1")
    _sub(seq, "name", f"{default_stem(plan)} · {output.id}")
    _sub(seq, "duration", total_f)
    _rate(seq, fps)
    tc = _sub(seq, "timecode")
    _rate(tc, fps)
    _sub(tc, "string", "00:00:00:00")
    _sub(tc, "frame", 0)
    _sub(tc, "displayformat", "NDF")
    media = _sub(seq, "media")
    video = _sub(media, "video")
    vf = _sub(video, "format")
    sc = _sub(vf, "samplecharacteristics")
    _rate(sc, fps)
    _sub(sc, "width", W)
    _sub(sc, "height", H)
    _sub(sc, "anamorphicpixelaspect", "1")
    _sub(sc, "pixelaspectratio", "square")
    _sub(sc, "fielddominance", "none")
    vtrack = _sub(video, "track")
    audio = _sub(media, "audio")
    _sub(audio, "numOutputChannels", 2)
    _sub(audio, "format")  # 占位，Premiere 会按项目设置
    file_ids: dict[str, str] = {}

    def file_el(parent: ET.Element, a: _Asset) -> None:
        if a.sid in file_ids:
            parent.append(ET.Element("file", id=file_ids[a.sid]))
            return
        fid = f"file-{len(file_ids) + 1}"
        file_ids[a.sid] = fid
        f = ET.SubElement(parent, "file", id=fid)
        _sub(f, "name", a.path.name)
        _sub(f, "pathurl", _pathurl(a.path))
        _rate(f, a.fps)
        _sub(f, "duration", int(a.info.duration * float(a.fps)) if a.kind != "image" else 1)
        m = _sub(f, "media")
        if a.info.video is not None or a.kind == "image":
            v = _sub(m, "video")
            c = _sub(v, "samplecharacteristics")
            _rate(c, a.fps)
            _sub(c, "width", a.disp[0])
            _sub(c, "height", a.disp[1])
        if a.info.audio:
            au = _sub(m, "audio")
            c = _sub(au, "samplecharacteristics")
            _sub(c, "depth", 16)
            _sub(c, "samplerate", a.info.audio[0].sample_rate)
            _sub(au, "channelcount", sum(x.channels for x in a.info.audio))

    # 每个保留片段：一个视频 clipitem + 选定音轨每个声道一个音频 clipitem，互相 link
    audio_tracks: dict[int, ET.Element] = {}
    for k, c in enumerate(clips, 1):
        a, s = c.asset, c.seg
        n_ch = a.info.audio[a.track].channels if a.info.audio and a.kind != "image" else 0
        vid = f"clipitem-v{k}"
        aud_ids = [f"clipitem-a{k}-{i}" for i in range(1, n_ch + 1)]
        links = [(vid, "video", 1)] + [(x, "audio", i) for i, x in enumerate(aud_ids, 1)]

        def common(item: ET.Element, name: str, kind: str, idx: int) -> None:
            _sub(item, "name", name)
            _sub(item, "enabled", "TRUE")
            _sub(item, "duration", int(a.info.duration * float(fps)) if a.kind != "image" else c.f1 - c.f0)
            _rate(item, fps)
            _sub(item, "start", c.f0)
            _sub(item, "end", c.f1)
            _sub(item, "in", c.in_f if a.kind != "image" else 0)
            _sub(item, "out", (c.in_f + (c.f1 - c.f0)) if a.kind != "image" else (c.f1 - c.f0))

        vi = ET.SubElement(vtrack, "clipitem", id=vid)
        common(vi, a.path.stem, "video", 1)
        file_el(vi, a)
        if abs(s.speed - 1.0) > 1e-9 or s.reverse:
            flt = _sub(_sub(vi, "filter"), "effect")
            _sub(flt, "name", "Time Remap")
            _sub(flt, "effectid", "timeremap")
            _sub(flt, "effectcategory", "motion")
            _sub(flt, "effecttype", "motion")
            _sub(flt, "mediatype", "video")
            p = _sub(flt, "parameter")
            _sub(p, "parameterid", "speed")
            _sub(p, "name", "speed")
            _sub(p, "value", f"{(-1 if s.reverse else 1) * s.speed * 100:g}")
        for i, (lid, mt, ti) in enumerate(links):
            ln = _sub(vi, "link")
            _sub(ln, "linkclipref", lid)
            _sub(ln, "mediatype", mt)
            _sub(ln, "trackindex", ti)
            _sub(ln, "clipindex", k)
        for i, aid in enumerate(aud_ids, 1):
            tr = audio_tracks.get(i)
            if tr is None:
                tr = _sub(audio, "track")
                audio_tracks[i] = tr
            ai = ET.SubElement(tr, "clipitem", id=aid)
            common(ai, a.path.stem, "audio", i)
            file_el(ai, a)
            st = _sub(ai, "sourcetrack")
            _sub(st, "mediatype", "audio")
            _sub(st, "trackindex", a.first_ch + i - 1)
            if s.mute:
                f = _sub(_sub(ai, "filter"), "effect")
                _sub(f, "name", "Audio Levels")
                _sub(f, "effectid", "audiolevels")
                p = _sub(f, "parameter")
                _sub(p, "parameterid", "level")
                _sub(p, "value", "0")
            for lid, mt, ti in links:
                ln = _sub(ai, "link")
                _sub(ln, "linkclipref", lid)
                _sub(ln, "mediatype", mt)
                _sub(ln, "trackindex", ti)
                _sub(ln, "clipindex", k)
    # 背景音：放到主轨音频之后的新音轨上
    for j, (b, a) in enumerate(beds):
        if not a.info.audio:
            continue
        f_at = int(round(b.at * float(fps)))
        if f_at >= total_f:
            continue
        length = (b.end - b.start) if b.end is not None else a.info.duration - b.start
        dur = min(int(round(length * float(fps))), total_f - f_at)
        n_ch = a.info.audio[a.track].channels
        for i in range(1, n_ch + 1):
            tr = _sub(audio, "track")
            bi = ET.SubElement(tr, "clipitem", id=f"clipitem-bed{j + 1}-{i}")
            _sub(bi, "name", a.path.stem)
            _sub(bi, "enabled", "TRUE")
            _sub(bi, "duration", int(a.info.duration * float(fps)))
            _rate(bi, fps)
            _sub(bi, "start", f_at)
            _sub(bi, "end", f_at + dur)
            _sub(bi, "in", int(round(b.start * float(fps))))
            _sub(bi, "out", int(round(b.start * float(fps))) + dur)
            file_el(bi, a)
            st = _sub(bi, "sourcetrack")
            _sub(st, "mediatype", "audio")
            _sub(st, "trackindex", a.first_ch + i - 1)
    if output.overlays:
        log.warning("%d 个叠加层（图片/文字）不随 xmeml 导出，需要在剪辑软件里重做", len(output.overlays))
    _write_xml(root, path, "xmeml")


# ================================================================== EDL（CMX3600）


def _tc(frames: int, nominal: int) -> str:
    f = frames % nominal
    s = frames // nominal
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def _write_edl(plan: Plan, output: Output, path: Path) -> None:
    fps, _size, clips, beds, assets = _prepare(plan, output)
    nominal = round(float(fps))
    lines = [f"TITLE: {default_stem(plan)} {output.id}", "FCM: NON-DROP FRAME", ""]
    for n, c in enumerate(clips, 1):
        a, s = c.asset, c.seg
        has_a = bool(a.info.audio) and a.kind != "image" and not s.mute
        track = "AA/V" if has_a else "V"
        src_len = c.f1 - c.f0
        lines.append(f"{n:03d}  AX       {track:<5} C        {_tc(c.in_f, nominal)} {_tc(c.in_f + src_len, nominal)} {_tc(c.f0, nominal)} {_tc(c.f1, nominal)}")
        if abs(s.speed - 1.0) > 1e-9:
            lines.append(f"M2   AX       {float(fps) * (-s.speed if s.reverse else s.speed):06.1f}                {_tc(c.in_f, nominal)}")
        lines.append(f"* FROM CLIP NAME: {a.path.name}")
        lines.append(f"* SOURCE FILE: {a.path}")
        if s.text:
            lines.append(f"* COMMENT: {s.text[:60]}")
    if beds:
        lines.append("* NOTE: 背景音/配音不在 EDL 里（CMX3600 只描述主轨剪辑点）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
