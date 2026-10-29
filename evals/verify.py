#!/usr/bin/env python
"""autocut eval 校验器：对照 fixtures 的 truth.json 判定一份剪辑产物是否合格。

baseline 和之后每个候选版 autocut 的 eval 共用本脚本，判定规则只在这里改。

用法：
    .venv/bin/python evals/verify.py --truth fixtures/media/talk_zh.truth.json \
        --source fixtures/media/talk_zh.mp4 --output out.mp4 --task long_pauses [--json] [-v]

--task：
    long_pauses    talk_zh：所有 speech 完整保留，无 >1.0s 残留间隔，挤在一起 (<0.08s) 的接缝 ≤1 处
    noisy_silence  lecture_en：同上；另外开头/结尾的残留静音 >1.0s 也算死寂
    retake_filler  talk_zh：无 tag 的 speech 完整保留；retake/filler 全部被剪掉
    export_nle     --output 是导出的工程文件（FCPXML/.fcpxmld/xmeml/CMX3600 EDL/OTIO）

语音片段存活检测：源和产物音频都解成 16kHz mono + 300–3400Hz 带通（不做预加重：-38dBFS 粉噪
经预加重放大后，AAC 二次编码会把 intact 的分数拉到 0.75–0.8），取源里每段 speech 做归一化互相关，
峰值 ≥0.8 = intact，< absent 阈值 = absent，之间 = partial（被剪进了语音）。
absent 阈值 = max(0.3, null_floor+0.03)：null_floor 是该模板在源音频里（排除自身落点）能匹配到的最高分，
短口头禅（0.23s 的“嗯”）会误匹配到 0.5 左右，固定的 0.3 会把剪干净的产物判成 partial。
另算 coverage（峰值落点上逐 50ms 帧的匹配能量占比），intact 但 coverage <0.97 记 clipped（头尾被削；理想剪辑经 aac32k/loudnorm/降噪后仍为 1.00，削掉 0.25s 句尾约 0.95）。

退出码：pass=0，fail=1，用法错误=2。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

log = logging.getLogger("verify")

SR = 16000
INTACT_MIN = 0.8
PARTIAL_MIN = 0.3
CLIPPED_COVERAGE = 0.97
FRAME = 800  # 50ms @16k，coverage 的帧长
FRAME_MATCH = 0.5
BAND_HZ = (300.0, 3400.0)
NULL_MARGIN = 0.03
NULL_CAP = 0.7  # null_floor 再高也不吞掉 partial 区间
NULL_GUARD_S = 0.25
ENERGY_FLOOR = 0.1  # 滑窗能量至少占模板能量的比例，避免零填充边缘/纯噪声窗口产生虚高分数
DEAD_AIR_S = 1.0
CHOPPY_S = 0.08
CHOPPY_MAX = 1
AV_DRIFT_MAX = 0.15
COVER_MIN = 0.9  # 导出时间线对每段该保留语音的源区间覆盖率下限
TASKS = ("long_pauses", "noisy_silence", "retake_filler", "export_nle")
MEDIA_TASKS = TASKS[:3]


# ---------------------------------------------------------------- truth / probe


def load_truth(path: Path) -> dict:
    truth = json.loads(path.read_text())
    speech = [s for s in truth["segments"] if s["kind"] == "speech"]
    truth["_speech"] = [{"idx": i, **s} for i, s in enumerate(speech)]
    return truth


def probe(path: Path) -> dict | None:
    """ffprobe；不可读返回 None。时长取各流 duration，缺失时退回容器 duration。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,duration,r_frame_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("ffprobe failed on %s: %s", path, r.stderr.strip()[:200])
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    fmt_dur = _f(data.get("format", {}).get("duration"))
    out = {"format_duration": fmt_dur, "video": None, "audio": None, "fps": None}
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind in ("video", "audio") and out[kind] is None:
            out[kind] = _f(s.get("duration")) or fmt_dur or 0.0
            if kind == "video":
                out["fps"] = _rate(s.get("r_frame_rate"))
    return out


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rate(s: str | None) -> float | None:
    if not s or "/" not in s:
        return _f(s)
    a, b = s.split("/")
    return float(a) / float(b) if float(b) else None


# ---------------------------------------------------------------- audio matching


def decode_audio(path: Path) -> np.ndarray:
    """ffmpeg pipe → 16kHz mono float64，再做 300–3400Hz 带通（FFT 掩码）。"""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {r.stderr.decode(errors='replace')[:200]}")
    x = np.frombuffer(r.stdout, dtype="<f4").astype(np.float64)
    if len(x) < 2:
        return x
    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(freq < BAND_HZ[0]) | (freq > BAND_HZ[1])] = 0
    return np.fft.irfft(spec, len(x))


def ncc_curve(template: np.ndarray, signal: np.ndarray) -> np.ndarray:
    """滑窗归一化互相关：分子用 FFT，分母用累加平方和。signal 已含零填充。
    滑窗能量 < ENERGY_FLOOR × 模板能量的位置记 0。"""
    n, m = len(template), len(signal)
    nfft = 1 << int(math.ceil(math.log2(m + n)))
    num = np.fft.irfft(np.fft.rfft(signal, nfft) * np.conj(np.fft.rfft(template, nfft)), nfft)[: m - n + 1]
    cs = np.concatenate([[0.0], np.cumsum(signal * signal)])
    win_e = np.maximum(cs[n:] - cs[:-n], 0.0)
    t_e = float(template @ template)
    score = np.zeros_like(num)
    ok = win_e >= ENERGY_FLOOR * t_e
    score[ok] = num[ok] / (np.sqrt(win_e[ok]) * math.sqrt(t_e))
    return score


def locate(template: np.ndarray, out: np.ndarray) -> dict:
    """在产物音频里找 template 的最佳落点。返回 score / out_start(秒，可为负) / coverage。"""
    n = len(template)
    if n == 0 or float(template @ template) < 1e-12:
        return {"score": 0.0, "out_start": None, "coverage": 0.0}
    padded = np.concatenate([np.zeros(n), out, np.zeros(n)])
    curve = ncc_curve(template, padded)
    k = int(np.argmax(curve))
    score = float(curve[k])
    seg = padded[k: k + n]
    # 逐帧匹配能量占比
    cov_num = cov_den = 0.0
    for a in range(0, n - FRAME + 1, FRAME):
        t, s = template[a: a + FRAME], seg[a: a + FRAME]
        te, se = float(t @ t), float(s @ s)
        cov_den += te
        if te > 0 and se > 0 and float(t @ s) / math.sqrt(te * se) >= FRAME_MATCH:
            cov_num += te
    return {"score": round(score, 4), "out_start": round((k - n) / SR, 4),
            "coverage": round(cov_num / cov_den, 4) if cov_den else 0.0}


def null_floor(template: np.ndarray, src: np.ndarray, start_s: float) -> float:
    """模板在源音频里、避开自身落点（±模板长度±NULL_GUARD_S）后的最高互相关：误匹配水平。"""
    n = len(template)
    if n == 0 or len(src) < n:
        return 0.0
    curve = ncc_curve(template, src)
    own = int(round(start_s * SR))
    guard = n + int(NULL_GUARD_S * SR)
    curve[max(0, own - guard): own + guard] = 0.0
    return float(curve.max())


def status_of(score: float, absent_below: float = PARTIAL_MIN) -> str:
    return "intact" if score >= INTACT_MIN else "partial" if score >= absent_below else "absent"


# ---------------------------------------------------------------- media tasks


def verify_media(truth: dict, source: Path, output: Path, task: str) -> dict:
    res: dict = {"task": task, "source": str(source), "output": str(output), "checks": {}, "reasons": []}
    checks, reasons = res["checks"], res["reasons"]

    if not output.is_file():
        checks["exists"] = False
        reasons.append("no_output: 产物文件不存在")
        res["pass"] = False
        return res
    checks["exists"] = True

    pr = probe(output)
    checks["probe_ok"] = pr is not None
    if pr is None:
        reasons.append("ffprobe 读不了产物")
        res["pass"] = False
        return res
    checks["has_video"] = pr["video"] is not None
    checks["has_audio"] = pr["audio"] is not None
    if not (checks["has_video"] and checks["has_audio"]):
        reasons.append("产物缺少视频流或音频流")
        res["pass"] = False
        return res
    drift = abs(pr["video"] - pr["audio"])
    checks["av_drift"] = round(drift, 4)
    checks["av_drift_ok"] = drift <= AV_DRIFT_MAX
    if not checks["av_drift_ok"]:
        reasons.append(f"音画时长差 {drift:.3f}s > {AV_DRIFT_MAX}s")

    src_pr = probe(source)
    src_dur = (src_pr or {}).get("audio") or truth.get("duration")
    res["durations"] = {"source": round(src_dur, 4), "output": round(max(pr["video"], pr["audio"]), 4),
                        "removed": round(src_dur - max(pr["video"], pr["audio"]), 4)}

    src_a, out_a = decode_audio(source), decode_audio(output)
    out_len = len(out_a) / SR
    segs = []
    for s in truth["_speech"]:
        tpl = src_a[int(round(s["start"] * SR)): int(round(s["end"] * SR))]
        loc = locate(tpl, out_a)
        floor = min(null_floor(tpl, src_a, s["start"]), NULL_CAP)
        absent_below = max(PARTIAL_MIN, floor + NULL_MARGIN)
        st = status_of(loc["score"], absent_below)
        segs.append({"idx": s["idx"], "start": s["start"], "end": s["end"], "dur": round(s["end"] - s["start"], 4),
                     "text": s.get("text", ""), "tag": s.get("tag"), "score": loc["score"], "status": st,
                     "absent_below": round(absent_below, 4),
                     "out_start": loc["out_start"], "coverage": loc["coverage"],
                     "clipped": st == "intact" and loc["coverage"] < CLIPPED_COVERAGE})
    res["segments"] = segs

    # 残留间隔：落在产物里的片段（intact + partial）按产物位置排序，相邻片段之间的距离
    alive = sorted((g for g in segs if g["status"] != "absent"), key=lambda g: g["out_start"])
    gaps = []
    for a, b in zip(alive, alive[1:]):
        gaps.append({"between": [a["idx"], b["idx"]], "at": round(a["out_start"] + a["dur"], 3),
                     "gap": round(b["out_start"] - (a["out_start"] + a["dur"]), 3)})
    res["gaps"] = gaps
    dead_air = [g for g in gaps if g["gap"] > DEAD_AIR_S]
    choppy = [g for g in gaps if g["gap"] < CHOPPY_S]
    res["dead_air"], res["choppy"] = dead_air, choppy
    if alive:
        res["edge_gaps"] = {"head": round(max(alive[0]["out_start"], 0.0), 3),
                            "tail": round(out_len - (alive[-1]["out_start"] + alive[-1]["dur"]), 3)}
    checks["dead_air_count"], checks["choppy_count"] = len(dead_air), len(choppy)

    if task in ("long_pauses", "noisy_silence"):
        bad = [g for g in segs if g["status"] != "intact"]
        clipped = [g for g in segs if g["clipped"]]
        checks["all_speech_intact"] = not bad
        checks["no_clipped_speech"] = not clipped
        checks["no_dead_air"] = not dead_air
        checks["choppy_ok"] = len(choppy) <= CHOPPY_MAX
        for g in bad:
            reasons.append(f"speech#{g['idx']}「{g['text'][:12]}」{g['status']}(score {g['score']})")
        for g in clipped:
            reasons.append(f"speech#{g['idx']}「{g['text'][:12]}」头尾被削(coverage {g['coverage']})")
        for g in dead_air:
            reasons.append(f"残留死寂 {g['gap']}s @产物 {g['at']}s")
        if len(choppy) > CHOPPY_MAX:
            reasons.append(f"{len(choppy)} 处接缝 <{CHOPPY_S}s（挤在一起）")
        if task == "noisy_silence" and alive:
            eg = res["edge_gaps"]
            edge_bad = [k for k, v in eg.items() if v > DEAD_AIR_S]
            checks["edges_trimmed"] = not edge_bad
            for k in edge_bad:
                reasons.append(f"{'开头' if k == 'head' else '结尾'}残留静音 {eg[k]}s")
        ok_keys = ["all_speech_intact", "no_clipped_speech", "no_dead_air", "choppy_ok", "av_drift_ok"]
        if task == "noisy_silence":
            ok_keys.append("edges_trimmed")
    else:  # retake_filler
        keep = [g for g in segs if not g["tag"]]
        drop = [g for g in segs if g["tag"]]
        checks["kept_intact"] = all(g["status"] == "intact" and not g["clipped"] for g in keep)
        checks["tagged_removed"] = all(g["status"] == "absent" for g in drop)
        for g in keep:
            if g["status"] != "intact":
                reasons.append(f"该保留的 speech#{g['idx']}「{g['text'][:12]}」{g['status']}(score {g['score']})")
            elif g["clipped"]:
                reasons.append(f"speech#{g['idx']}「{g['text'][:12]}」头尾被削(coverage {g['coverage']})")
        for g in drop:
            if g["status"] != "absent":
                reasons.append(f"该剪掉的 {g['tag']} speech#{g['idx']}「{g['text'][:12]}」仍在({g['status']}, score {g['score']})")
        ok_keys = ["kept_intact", "tagged_removed", "av_drift_ok"]

    res["pass"] = all(checks[k] for k in ok_keys) and checks["has_video"] and checks["has_audio"]
    return res


# ---------------------------------------------------------------- NLE export


def _secs(s: str | None, default: float = 0.0) -> float:
    """FCPXML 有理数时间 "300/30s" / "5s" / "1.5s"。"""
    if s is None:
        return default
    s = s.strip().rstrip("s")
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b)
    return float(s)


def _tc(tc: str, fps: float) -> float:
    h, m, s, f = (int(x) for x in re.split(r"[:;]", tc))
    nominal = round(fps)
    return ((h * 3600 + m * 60 + s) * nominal + f) / fps


def parse_fcpxml(root: ET.Element) -> dict:
    spine = root.find(".//spine")
    clips, cursor, end = [], 0.0, 0.0
    if spine is not None:
        for el in spine:
            if el.get("lane") or el.get("duration") is None:
                continue
            dur = _secs(el.get("duration"))
            off = _secs(el.get("offset"), cursor)
            cursor = off + dur
            end = max(end, cursor)
            if el.tag in ("asset-clip", "clip", "video", "audio", "ref-clip", "sync-clip", "mc-clip"):
                st = _secs(el.get("start"), 0.0)
                clips.append({"tl": (off, off + dur), "src": (st, st + dur)})
    seq = root.find(".//sequence")
    seq_dur = _secs(seq.get("duration")) if seq is not None and seq.get("duration") else None
    return {"format": "fcpxml", "events": len(clips), "timeline_duration": end or seq_dur,
            "sequence_duration_attr": seq_dur, "clips": clips}


def _xmeml_fps(rate: ET.Element | None, default: float) -> float:
    if rate is None or rate.findtext("timebase") is None:
        return default
    tb = float(rate.findtext("timebase"))
    return tb * 1000 / 1001 if (rate.findtext("ntsc") or "").upper() == "TRUE" else tb


def parse_xmeml(root: ET.Element) -> dict:
    seq = root.find(".//sequence")
    if seq is None:
        return {"format": "xmeml", "events": 0, "timeline_duration": None, "clips": []}
    seq_fps = _xmeml_fps(seq.find("rate"), 30.0)
    items = seq.findall("./media/video//clipitem") or seq.findall("./media/audio//clipitem")
    clips, end = [], 0.0
    for it in items:
        fps = _xmeml_fps(it.find("rate"), seq_fps)
        st, en, i, o = (_f(it.findtext(k)) for k in ("start", "end", "in", "out"))
        if st is None or en is None or st < 0 or en < 0:
            continue
        end = max(end, en / seq_fps)
        if i is not None and o is not None and o > i:
            clips.append({"tl": (st / seq_fps, en / seq_fps), "src": (i / fps, o / fps)})
        else:
            clips.append({"tl": (st / seq_fps, en / seq_fps), "src": None})
    seq_dur = _f(seq.findtext("duration"))
    return {"format": "xmeml", "events": len(clips), "timeline_duration": end or (seq_dur / seq_fps if seq_dur else None),
            "sequence_duration_attr": seq_dur / seq_fps if seq_dur else None, "clips": clips}


_TC = r"(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)"
_EDL_EVENT = re.compile(r"^\s*(\d{1,6})\s+(\S+)\s+(\S+)\s+(\S+)\s+(?:\d+\s+)?" + r"\s+".join([_TC] * 4), re.M)


def parse_edl(text: str, fps: float) -> dict:
    clips = []
    for m in _EDL_EVENT.finditer(text):
        si, so, ri, ro = (_tc(m.group(g), fps) for g in (5, 6, 7, 8))
        if m.group(3).upper().startswith("A") and not m.group(3).upper().startswith("AA"):
            kind = "A"
        else:
            kind = "V"
        clips.append({"tl": (ri, ro), "src": (si, so), "track": kind})
    vids = [c for c in clips if c["track"] == "V"] or clips
    dur = (max(c["tl"][1] for c in vids) - min(c["tl"][0] for c in vids)) if vids else None
    return {"format": "edl", "events": len(clips), "timeline_duration": dur, "clips": vids}


def parse_otio(data: dict) -> dict:
    def rt(d):
        return d["value"] / d["rate"]
    clips, cursor = [], 0.0
    tracks = data.get("tracks", {}).get("children", [])
    track = next((t for t in tracks if t.get("kind") == "Video"), tracks[0] if tracks else None)
    for ch in (track or {}).get("children", []):
        sr = ch.get("source_range")
        if not sr:
            continue
        d = rt(sr["duration"])
        if ch.get("OTIO_SCHEMA", "").startswith("Clip"):
            st = rt(sr["start_time"])
            clips.append({"tl": (cursor, cursor + d), "src": (st, st + d)})
        cursor += d
    return {"format": "otio", "events": len(clips), "timeline_duration": cursor or None, "clips": clips}


def _union_cover(ranges: list[tuple[float, float]], a: float, b: float) -> float:
    """ranges 的并集与 [a,b] 的交集长度。"""
    total, last = 0.0, a
    for s, e in sorted(ranges):
        s, e = max(s, last), min(e, b)
        if e > s:
            total += e - s
            last = e
    return total


def verify_export(truth: dict, source: Path, output: Path) -> dict:
    res: dict = {"task": "export_nle", "source": str(source), "output": str(output), "checks": {}, "reasons": []}
    checks, reasons = res["checks"], res["reasons"]
    path = output
    if path.is_dir():  # .fcpxmld 包
        path = path / "Info.fcpxml"
    if not path.is_file():
        checks["exists"] = False
        reasons.append("no_output: 工程文件不存在")
        res["pass"] = False
        return res
    checks["exists"] = True
    text = path.read_text(errors="replace")
    sniff = text.lstrip()[:1]
    fps = (probe(source) or {}).get("fps") or truth.get("fps") or 30.0
    try:
        if sniff == "<":
            root = ET.fromstring(text.encode())
            tl = {"fcpxml": parse_fcpxml, "xmeml": parse_xmeml}.get(root.tag)
            if tl is None:
                raise ValueError(f"未知 XML 根元素 <{root.tag}>（只认 fcpxml / xmeml）")
            info = tl(root)
        elif sniff == "{":
            info = parse_otio(json.loads(text))
        else:
            info = parse_edl(text, fps)
            if not info["events"]:
                raise ValueError("EDL 里解析不出任何事件")
        checks["parse_ok"] = True
    except (ET.ParseError, ValueError, KeyError, json.JSONDecodeError, ZeroDivisionError) as e:
        checks["parse_ok"] = False
        reasons.append(f"解析失败: {type(e).__name__}: {str(e)[:160]}")
        res["pass"] = False
        return res

    res["format"] = info["format"]
    res["events"] = info["events"]
    res["timeline_duration"] = round(info["timeline_duration"], 4) if info["timeline_duration"] else None
    res["sequence_duration_attr"] = info.get("sequence_duration_attr")
    checks["has_events"] = info["events"] > 0
    if not checks["has_events"]:
        reasons.append("时间线上没有任何片段")

    stem, name = source.stem, source.name
    ref = name in text or (info["format"] == "edl" and stem in text)
    checks["source_ref"] = ref
    if not ref:
        reasons.append(f"工程文件没有引用源媒体 {name}")

    kept = sum(s["end"] - s["start"] for s in truth["_speech"] if not s.get("tag"))
    orig = truth.get("duration") or (probe(source) or {}).get("audio")
    lo, hi = kept, orig - DEAD_AIR_S
    res["duration_bounds"] = [round(lo, 4), round(hi, 4)]
    td = info["timeline_duration"]
    checks["duration_in_range"] = td is not None and lo <= td <= hi
    if not checks["duration_in_range"]:
        reasons.append(f"时间线总长 {td if td is None else round(td, 2)}s 不在 [{lo:.2f}, {hi:.2f}]")

    srcs = [c["src"] for c in info["clips"] if c.get("src")]
    if srcs:
        cover = []
        for s in truth["_speech"]:
            if s.get("tag"):
                continue
            frac = _union_cover(srcs, s["start"], s["end"]) / (s["end"] - s["start"])
            cover.append({"idx": s["idx"], "text": s.get("text", ""), "covered": round(frac, 3)})
        res["speech_cover"] = cover
        checks["speech_covered"] = all(c["covered"] >= COVER_MIN for c in cover)
        for c in cover:
            if c["covered"] < COVER_MIN:
                reasons.append(f"该保留的 speech#{c['idx']}「{c['text'][:12]}」时间线只覆盖 {c['covered']:.0%}")
        # 长停顿区间在时间线里被保留的长度
        kept_gap = []
        for g in truth["segments"]:
            if g["kind"] == "gap" and g["end"] - g["start"] > DEAD_AIR_S:
                c = _union_cover(srcs, g["start"], g["end"])
                kept_gap.append({"start": g["start"], "end": g["end"], "kept": round(c, 3)})
        res["long_gap_kept"] = kept_gap
        checks["no_dead_air"] = all(k["kept"] <= DEAD_AIR_S for k in kept_gap)
        for k in kept_gap:
            if k["kept"] > DEAD_AIR_S:
                reasons.append(f"长停顿 {k['start']}–{k['end']}s 在时间线里保留了 {k['kept']}s")
    else:
        res["speech_cover"] = None  # 解析不出源区间：不参与判定

    gate = ["parse_ok", "has_events", "source_ref", "duration_in_range"] + \
           [k for k in ("speech_covered", "no_dead_air") if k in checks]
    res["pass"] = all(checks[k] for k in gate)
    return res


# ---------------------------------------------------------------- entry


def verify(truth_path: Path, source: Path, output: Path, task: str) -> dict:
    truth = load_truth(truth_path)
    res = verify_export(truth, source, output) if task == "export_nle" else verify_media(truth, source, output, task)
    res["pass"] = bool(res["pass"])
    return res


def human(res: dict) -> str:
    L = [f"[{res['task']}] {'PASS' if res['pass'] else 'FAIL'}  output={res['output']}"]
    if "durations" in res:
        d = res["durations"]
        L.append(f"  时长 源 {d['source']}s → 产物 {d['output']}s（剪掉 {d['removed']}s）")
    for g in res.get("segments", []):
        tag = f" [{g['tag']}]" if g["tag"] else ""
        L.append(f"  speech#{g['idx']:<2} {g['start']:>7.2f}–{g['end']:<7.2f} {g['status']:<7} score={g['score']:.3f} "
                 f"cov={g['coverage']:.2f} floor<{g['absent_below']:.2f} @out={g['out_start']}{' CLIPPED' if g['clipped'] else ''}{tag} {g['text'][:14]}")
    if res.get("gaps") is not None:
        L.append("  相邻片段间隔: " + ", ".join(f"{g['gap']}s" for g in res["gaps"]))
    if "edge_gaps" in res:
        L.append(f"  首尾静音: head={res['edge_gaps']['head']}s tail={res['edge_gaps']['tail']}s")
    if "format" in res:
        L.append(f"  格式 {res['format']}，事件 {res['events']}，时间线 {res['timeline_duration']}s，"
                 f"要求范围 {res.get('duration_bounds')}")
    L.append("  checks: " + ", ".join(f"{k}={v}" for k, v in res["checks"].items()))
    for r in res["reasons"]:
        L.append(f"  ✗ {r}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--truth", required=True, type=Path)
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--task", required=True, choices=TASKS)
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    for p, label in ((a.truth, "--truth"), (a.source, "--source")):
        if not p.is_file():
            print(f"usage error: {label} 文件不存在: {p}", file=sys.stderr)
            return 2
    res = verify(a.truth, a.source, a.output, a.task)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else human(res))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
