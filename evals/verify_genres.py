#!/usr/bin/env python
"""autocut eval 校验器（第二批：各视频类型）。复用 verify.py 的互相关函数，不改它的接口。

用法：
    .venv/bin/python evals/verify_genres.py --truth fixtures/media/<name>.truth.json \
        --source <源文件或目录> --output <产物文件或目录> --task <task> [--json] [-v]

--task / --source / --output：
    game_commentary  --source game_commentary.mp4            --output game_cut.mp4
    stream_clips     --source stream_vod.mp4                 --output <含 clip_1/2/3.mp4 的目录>
    montage          --source fixtures/media/montage/        --output montage.mp4
    knowledge        --source fixtures/media/knowledge/      --output knowledge.mp4
    ytpmv            --source fixtures/media/ytpmv/          --output ytpmv.mp4

判定（pass = 下面标 [gate] 的全部通过；其余只记录）：
  game_commentary  [gate] 视频+音频、音画差 ≤0.15s、所有 speech intact（a:0 混音）、所有爆炸事件在产物里、
                   每个 boring 间隔残留 ≤1.0s（+1 帧容差；锚点法：用 intact 的语音和在场的爆炸事件做锚点，
                   残留 = 相邻锚点在产物里的间距 − 源里除该间隔外的间距；不用逐窗口匹配，
                   因为游戏音轨是周期循环音乐，任何窗口都会在别处误匹配）
  stream_clips     [gate] 恰好 clip_1..3.mp4（无多余 clip_N.mp4）、每个 15–30s、9:16(±0.01)、有音频、
                   逐 3s 窗口（步长 1.5s）回源里互相关定位，取唯一匹配的窗口按偏移聚类，被匹配窗口的并集覆盖某个 highlight ≥80%（±0.25s），
                   3 个切片对应 3 个不同 highlight；匹配不上的窗口（片头音效、响度处理、BGM、静音段）只记录不判失败
                   字幕：有字幕流或同名 .srt/.ass/.vtt 记 verified，否则 burned_or_missing（只记录）
  montage          [gate] 12–18s、有 BGM（产物音频与 BGM 互相关 ≥0.6）、画面切点（scene>0.3）≥4 个、
                   落在拍点/半拍 ±34ms 内的比例 ≥80%
  knowledge        [gate] 横屏、时长在参考配音时长的 0.7–1.6 倍、非静音占比 ≥50%、配图 ≥2 张出现（RGB 64×36 MSE）
                   字幕：同 stream_clips，另外文本与脚本字符重合 ≥60% 才算 verified（只记录）
  ytpmv            [gate] 产物含 BGM（互相关 ≥0.4，全频段/低频段取高）、truth 音节在产物里出现 ≥6 次（互相关 ≥0.6，非极大值抑制）、
                   出现点落在拍点/半拍 ±50ms 内的比例 ≥70%（“出现点”取音节起点或能量起音点，较高者；
                   能量起音 = 包络达峰值 25% 的时刻，比含辅音的音节起点晚 0–110ms，agent 常把它对到拍点）；
                   补充：必须有音节被重复（出现次数 > 不同音节数），否则把原片放一遍也有 12 次

所有产物另记 loudness（ebur128 积分响度 / 真峰值），供人工判断成片质量。
退出码：pass=0，fail=1，用法错误=2。
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

import verify as V

log = logging.getLogger("verify_genres")

TASKS = ("game_commentary", "stream_clips", "montage", "knowledge", "ytpmv")
BGM_BAND = (100.0, 7500.0)
BGM_LOW_BAND = (40.0, 180.0)  # 鼓点/低音：人声弱、滤波/变调/压缩引入的相位偏移小，对处理过的 BGM 更稳
AV_DRIFT_MAX = V.AV_DRIFT_MAX
SUB_EXTS = (".srt", ".ass", ".vtt")

# game_commentary
BORING_RESIDUAL_MAX = 1.0
BORING_TOL_S = 1 / 30  # 一帧容差：残留 1.005s 这种几毫秒的超出是测量/取整噪声，不是没剪
ANCHOR_EPS_S = 0.05
# stream_clips
CLIP_DUR = (15.0, 30.0)
VERTICAL = 9 / 16
VERTICAL_TOL = 0.01
HIGHLIGHT_TOL_S = 0.25
CONTIGUOUS_TOL_S = 0.1
CLIP_WIN_S, CLIP_HOP_S = 3.0, 1.5
HIGHLIGHT_COVER_MIN = 0.8  # 被匹配窗口的并集需覆盖 highlight 的比例
UNIQUE_MIN = 0.8
CLIP_MATCH_MIN = 0.5
# montage
MONTAGE_DUR = (12.0, 18.0)
BGM_MONTAGE_MIN = 0.6
SCENE_THR = 0.3
CUT_MIN = 4
CUT_TOL_S = 0.034
CUT_ALIGNED_MIN = 0.8
# knowledge
NARRATION_RATIO = (0.7, 1.6)
NONSILENT_MIN = 0.5
SILENCE_DB = -40.0
IMG_MSE_USED = 0.01
IMAGES_MIN = 2
SUB_TEXT_MIN = 0.6
# ytpmv
BGM_YTPMV_MIN = 0.4  # 规格 0.5；实测：未处理的 BGM 低频段 0.97–0.99，压低/变调处理过的 0.47，没有 BGM 的 0.12，全频段对处理过的失灵（0.08–0.26）
SYL_MIN = 0.6
SYL_COUNT_MIN = 6
SYL_TOL_S = 0.05
SYL_ALIGNED_MIN = 0.7
SYL_NMS_S = 0.12
ONSET_FRAC = 0.25  # 能量起音点：5ms 包络达到该音节峰值 25% 的时刻（元音/能量起音，通常比含辅音的音节起点晚 0–110ms）


# ---------------------------------------------------------------- 通用：probe / 解码 / 响度 / 字幕


def probe_full(path: Path) -> dict | None:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,width,height,duration,start_time,r_frame_rate:format=duration,start_time",
         "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    fmt = d.get("format", {})
    fdur = V._f(fmt.get("duration"))
    out = {"format_duration": fdur, "video": None, "audio": [], "subtitle": []}
    for s in d.get("streams", []):
        info = {"index": s.get("index"), "codec": s.get("codec_name"),
                "duration": V._f(s.get("duration")) or fdur or 0.0, "start": V._f(s.get("start_time")) or 0.0}
        kind = s.get("codec_type")
        if kind == "video" and out["video"] is None:
            info.update(width=s.get("width"), height=s.get("height"), fps=V._rate(s.get("r_frame_rate")))
            out["video"] = info
        elif kind == "audio":
            out["audio"].append(info)
        elif kind == "subtitle":
            out["subtitle"].append(info)
    return out


def decode(path: Path, stream: int = 0, sr: int = V.SR, band: tuple[float, float] | None = V.BAND_HZ) -> np.ndarray:
    """第 stream 条音轨 → sr Hz mono float64，可选带通（FFT 掩码）。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{stream}", "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed ({path.name} a:{stream}): {r.stderr.decode(errors='replace')[:200]}")
    x = np.frombuffer(r.stdout, dtype="<f4").astype(np.float64)
    if band is None or len(x) < 2:
        return x
    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(len(x), 1 / sr)
    spec[(freq < band[0]) | (freq > band[1])] = 0
    return np.fft.irfft(spec, len(x))


def loudness(path: Path) -> dict:
    """ebur128 积分响度（LUFS）和真峰值（dBFS）。仅记录。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True)
    i = re.findall(r"\bI:\s+(-?(?:inf|[\d.]+))\s+LUFS", r.stderr)
    p = re.findall(r"\bPeak:\s+(-?(?:inf|[\d.]+))\s+dBFS", r.stderr)
    return {"lufs": float(i[-1]) if i else None, "true_peak_dbfs": float(p[-1]) if p else None}


def frames_rgb(path: Path, fps: float, w: int = 64, h: int = 36) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf", f"fps={fps},scale={w}:{h}:flags=area,format=rgb24",
                        "-f", "rawvideo", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return np.zeros((0, h, w, 3))
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, 3) / 255.0


def image_rgb(path: Path, w: int = 64, h: int = 36) -> np.ndarray | None:
    """单张图 → (h, w, 3) float。fps 滤镜对单帧输入会吐 0 帧，所以单独走 -frames:v 1。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf", f"scale={w}:{h}:flags=area,format=rgb24",
                        "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) != w * h * 3:
        return None
    return np.frombuffer(r.stdout, np.uint8).reshape(h, w, 3) / 255.0


def clean_text(s: str) -> str:
    return re.sub(r"[\W_]+", "", s)


def parse_sub_text(text: str) -> str:
    """SRT / VTT / ASS 去掉时间码、序号、样式标记后的纯文本。"""
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("Dialogue:"):
            ln = ln.split(",", 9)[-1]
        elif re.match(r"^(\d+|WEBVTT.*|NOTE.*|\[.*\]|Format:.*|Style:.*|Comment:.*)$", ln) or "-->" in ln:
            continue
        ln = re.sub(r"\{[^}]*\}|<[^>]+>|\\N", " ", ln)
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def find_subtitles(video: Path, pf: dict | None) -> dict:
    """字幕来源：mp4 里的字幕流 / 同名侧车文件 / 都没有（burned_or_missing，无法判断是否烧录）。"""
    for ext in SUB_EXTS:
        side = video.with_suffix(ext)
        if side.is_file():
            return {"status": "verified", "where": "sidecar", "file": side.name,
                    "text": parse_sub_text(side.read_text(errors="replace"))}
    if pf and pf["subtitle"]:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:s:0", "-f", "srt", "-"],
                           capture_output=True, text=True)
        return {"status": "verified", "where": "stream", "file": None, "text": parse_sub_text(r.stdout)}
    return {"status": "burned_or_missing", "where": None, "file": None, "text": ""}


def char_overlap(reference: str, candidate: str) -> float:
    """reference 的字符被 candidate 覆盖的比例（按字符多重集）。"""
    from collections import Counter
    ref, cand = Counter(clean_text(reference)), Counter(clean_text(candidate))
    total = sum(ref.values())
    return sum(min(n, cand[c]) for c, n in ref.items()) / total if total else 0.0


def base_checks(res: dict, output: Path, need_audio: bool = True) -> dict | None:
    """exists / probe / 视频+音频 / 音画差。失败返回 None（res 已填好 reasons 和 pass=False）。"""
    checks, reasons = res["checks"], res["reasons"]
    if not output.is_file():
        checks["exists"] = False
        reasons.append(f"no_output: 产物 {output.name} 不存在")
        res["pass"] = False
        return None
    checks["exists"] = True
    pf = probe_full(output)
    checks["probe_ok"] = pf is not None
    if pf is None:
        reasons.append("ffprobe 读不了产物")
        res["pass"] = False
        return None
    checks["has_video"] = pf["video"] is not None
    checks["has_audio"] = bool(pf["audio"])
    if not checks["has_video"] or (need_audio and not checks["has_audio"]):
        reasons.append("产物缺少视频流" if not checks["has_video"] else "产物没有音频流")
        res["pass"] = False
        return None
    dur = max([pf["video"]["duration"]] + [a["duration"] for a in pf["audio"]])
    res["output_info"] = {"duration": round(dur, 3), "width": pf["video"]["width"], "height": pf["video"]["height"],
                          "audio_streams": len(pf["audio"]), "subtitle_streams": len(pf["subtitle"])}
    if pf["audio"]:
        drift = abs(pf["video"]["duration"] - pf["audio"][0]["duration"])
        checks["av_drift"] = round(drift, 4)
        checks["av_drift_ok"] = drift <= AV_DRIFT_MAX
        if not checks["av_drift_ok"]:
            reasons.append(f"音画时长差 {drift:.3f}s > {AV_DRIFT_MAX}s")
    res["loudness"] = loudness(output)
    return pf


def new_result(task: str, source: Path, output: Path) -> dict:
    return {"task": task, "source": str(source), "output": str(output), "checks": {}, "reasons": []}


def finish(res: dict, gate: list[str]) -> dict:
    res["pass"] = all(res["checks"].get(k) is True for k in gate)
    return res


# ---------------------------------------------------------------- 互相关工具


def score_segments(segs: list[dict], src_a: np.ndarray, out_a: np.ndarray) -> list[dict]:
    """逐段在 out_a 里找 src_a 切片（同 verify.py 的 intact/partial/absent/clipped 规则）。"""
    rows = []
    for s in segs:
        tpl = src_a[int(round(s["start"] * V.SR)): int(round(s["end"] * V.SR))]
        loc = V.locate(tpl, out_a)
        floor = min(V.null_floor(tpl, src_a, s["start"]), V.NULL_CAP)
        below = max(V.PARTIAL_MIN, floor + V.NULL_MARGIN)
        st = V.status_of(loc["score"], below)
        rows.append({"start": s["start"], "end": s["end"], "text": s.get("text", ""), "tag": s.get("tag"),
                     "score": loc["score"], "status": st, "out_start": loc["out_start"], "coverage": loc["coverage"],
                     "clipped": st == "intact" and loc["coverage"] < V.CLIPPED_COVERAGE})
    return rows


def gap_residuals(truth: dict, speech_rows: list[dict], events: list[dict], out_dur: float) -> list[dict]:
    """每个 gap 在产物里残留多少秒。锚点 = intact 的语音 + 在场的事件；
    残留 = (后锚点产物起点 − 前锚点产物终点) − (源里两锚点间除该 gap 外的间距)，截断到 [0, gap 长度]。"""
    anchors = [{"src0": r["start"], "src1": r["end"], "out0": r["out_start"], "out1": r["out_start"] + r["end"] - r["start"]}
               for r in speech_rows if r["status"] == "intact"]
    anchors += [{"src0": e["start"], "src1": e["end"], "out0": e["out_start"], "out1": e["out_start"] + e["end"] - e["start"]}
                for e in events if e["present"]]
    anchors.sort(key=lambda x: x["src0"])
    src_dur = truth.get("duration") or truth["segments"][-1]["end"]
    rows = []
    for g in truth["segments"]:
        if g["kind"] != "gap":
            continue
        prev = [x for x in anchors if x["src1"] <= g["start"] + ANCHOR_EPS_S]
        nxt = [x for x in anchors if x["src0"] >= g["end"] - ANCHOR_EPS_S]
        p = prev[-1] if prev else {"src1": 0.0, "out1": 0.0}
        n = nxt[0] if nxt else {"src0": src_dur, "out0": out_dur}
        glen = g["end"] - g["start"]
        kept = (n["out0"] - p["out1"]) - ((n["src0"] - p["src1"]) - glen)
        rows.append({"start": g["start"], "end": g["end"], "tag": g.get("tag"), "len": round(glen, 3),
                     "kept": round(min(max(kept, 0.0), glen), 3)})
    return rows


def bgm_alignment(out_a: np.ndarray, bgm_a: np.ndarray, win_s: float = 3.0, n_win: int = 6) -> dict:
    """产物音频里均匀取 n_win 个 win_s 窗口回 BGM 里定位，取互相关最高的窗口得 BGM 起点偏移。"""
    L = min(int(win_s * V.SR), len(out_a))
    if L < V.SR // 2 or len(bgm_a) < L:
        return {"score": 0.0, "offset": None, "windows": []}
    rows = []
    for s in np.linspace(0, len(out_a) - L, n_win).astype(int):
        tpl = out_a[s: s + L]
        loc = V.locate(tpl, bgm_a) if float(tpl @ tpl) > 1e-12 else {"score": 0.0, "out_start": None}
        pos = loc["out_start"]
        rows.append({"win_start": round(s / V.SR, 3), "score": loc["score"], "bgm_pos": pos,
                     "offset": None if pos is None else round(pos - s / V.SR, 4)})
    best = max(rows, key=lambda r: r["score"])
    return {"score": best["score"], "offset": best["offset"], "windows": rows}


def locate_candidates(tpl: np.ndarray, sig: np.ndarray, thr: float = 0.5, rel: float = 0.03, nms_s: float = 0.2,
                      k: int = 16) -> list[tuple[float, float]]:
    """tpl 在 sig 里所有「分数 ≥ max(thr, 最高分-rel)」的位置 [(起点秒, 分数)]。背景音是周期循环时，
    安静窗口会在多处同分匹配，只取最高峰会定位错。"""
    if len(sig) < len(tpl) or float(tpl @ tpl) < 1e-12:
        return []
    curve = V.ncc_curve(tpl, sig).copy()
    best = float(curve.max())
    if best < thr:
        return []
    out, half = [], int(nms_s * V.SR)
    while len(out) < k:
        i = int(np.argmax(curve))
        if curve[i] < max(thr, best - rel):
            break
        out.append((i / V.SR, float(curve[i])))
        curve[max(0, i - half): i + half] = 0.0
    return out


def clip_windows(clip_a: np.ndarray, src_a: np.ndarray) -> list[dict]:
    """把片段音频切成 3s 窗口（步长 1.5s），每个窗口回源里找所有候选位置。"""
    L, hop = int(CLIP_WIN_S * V.SR), int(CLIP_HOP_S * V.SR)
    out = []
    for st in range(0, max(len(clip_a) - L, 0) + 1, hop):
        tpl = clip_a[st: st + L]
        if len(tpl) < L:
            break
        cands = locate_candidates(tpl, src_a, CLIP_MATCH_MIN)
        out.append({"t": st / V.SR, "cands": [(round(pos - st / V.SR, 3), round(sc, 3)) for pos, sc in cands]})
    return out


def cluster_offsets(wins: list[dict]) -> list[dict]:
    """唯一匹配（只有一个候选且分数 ≥0.8）的窗口按「片段起点在源里的偏移」聚类；每个聚类的覆盖区间 =
    支持它的窗口（含候选里有该偏移的歧义窗口）对应的源区间并集。周期背景音的安静窗口有多个候选，只能当支持者、不能当发起者。"""
    clusters: list[dict] = []
    for w in wins:
        if len(w["cands"]) == 1 and w["cands"][0][1] >= UNIQUE_MIN:
            off = w["cands"][0][0]
            for c in clusters:
                if abs(c["offset"] - off) <= CONTIGUOUS_TOL_S:
                    break
            else:
                clusters.append({"offset": off})
    for c in clusters:
        sup = [w for w in wins if any(abs(o - c["offset"]) <= CONTIGUOUS_TOL_S for o, _ in w["cands"])]
        c["n"] = len(sup)
        c["n_unique"] = sum(1 for w in sup if len(w["cands"]) == 1)
        c["covered"] = [(c["offset"] + w["t"], c["offset"] + w["t"] + CLIP_WIN_S) for w in sup]
    return [c for c in clusters if c["n_unique"] >= 2]  # 至少 2 个唯一匹配窗口，歧义窗口只能当支持者，防止伪聚类凑出虚假覆盖


def bgm_match(output: Path, bgm_path: Path) -> dict:
    """产物音频与 BGM 的互相关：全频段和低频段各算一遍，取得分高者（给出 BGM 偏移）。
    变调、压低、响度处理、滤波之后全频段波形互相关会塌（0.1 左右），低频段仍然稳。"""
    full = bgm_alignment(decode(output, 0, band=BGM_BAND), decode(bgm_path, 0, band=BGM_BAND))
    low = bgm_alignment(decode(output, 0, band=BGM_LOW_BAND), decode(bgm_path, 0, band=BGM_LOW_BAND))
    best, band = (low, "low") if low["score"] > full["score"] else (full, "full")
    return {**best, "band": band, "score_full": full["score"], "score_low": low["score"]}


def scene_cuts(path: Path, thr: float = SCENE_THR) -> list[float]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf",
                        f"select='gt(scene,{thr})',showinfo", "-f", "null", "-"], capture_output=True, text=True)
    return [float(t) for t in re.findall(r"pts_time:(-?[\d.]+)", r.stderr) if float(t) > 0.1]


def grid_err(t: float, step: float) -> float:
    return abs(t - round(t / step) * step)


# ---------------------------------------------------------------- game_commentary


def verify_game(truth: dict, source: Path, output: Path) -> dict:
    res = new_result("game_commentary", source, output)
    checks, reasons = res["checks"], res["reasons"]
    if base_checks(res, output) is None:
        return res
    src_a, out_a = decode(source, 0), decode(output, 0)

    speech = [s for s in truth["segments"] if s["kind"] == "speech"]
    res["speech"] = rows = score_segments(speech, src_a, out_a)
    bad = [r for r in rows if r["status"] != "intact" or r["clipped"]]
    checks["all_speech_intact"] = not bad
    for r in bad:
        reasons.append(f"speech「{r['text'][:12]}」{r['status']}{'(头尾被削)' if r['clipped'] else ''}(score {r['score']})")

    events = []
    for e in truth.get("events", []):
        tpl = src_a[int(round(e["start"] * V.SR)): int(round(e["end"] * V.SR))]
        loc = V.locate(tpl, out_a)
        events.append({**e, "score": loc["score"], "present": loc["score"] >= V.INTACT_MIN, "out_start": loc["out_start"]})
    res["events"] = events
    checks["all_events_present"] = all(e["present"] for e in events)
    for e in events:
        if not e["present"]:
            reasons.append(f"{e['kind']} {e['start']}–{e['end']}s 不在产物里(score {e['score']})")

    gaps = gap_residuals(truth, rows, events, res["output_info"]["duration"])
    res["gaps"] = gaps
    boring = [g for g in gaps if g["tag"] == "boring"]
    checks["boring_residual_ok"] = all(g["kept"] <= BORING_RESIDUAL_MAX + BORING_TOL_S for g in boring)
    for g in boring:
        if g["kept"] > BORING_RESIDUAL_MAX + BORING_TOL_S:
            reasons.append(f"boring 间隔 {g['start']}–{g['end']}s 残留 {g['kept']}s > {BORING_RESIDUAL_MAX}s")
    src_dur = truth.get("duration") or len(src_a) / V.SR
    res["durations"] = {"source": round(src_dur, 3), "output": res["output_info"]["duration"],
                        "removed": round(src_dur - res["output_info"]["duration"], 3)}
    return finish(res, ["av_drift_ok", "all_speech_intact", "all_events_present", "boring_residual_ok"])


# ---------------------------------------------------------------- stream_clips


def verify_clips(truth: dict, source: Path, out_dir: Path) -> dict:
    res = new_result("stream_clips", source, out_dir)
    checks, reasons = res["checks"], res["reasons"]
    if not out_dir.is_dir():
        checks["exists"] = False
        reasons.append("no_output: --output 应是包含 clip_1..3.mp4 的目录")
        res["pass"] = False
        return res
    names = [f"clip_{i}.mp4" for i in (1, 2, 3)]
    found = [n for n in names if (out_dir / n).is_file()]
    extras = sorted(p.name for p in out_dir.glob("clip_*.mp4") if p.name not in names)
    checks["exists"] = bool(found)
    checks["exactly_three_files"] = len(found) == 3 and not extras
    if len(found) != 3:
        reasons.append(f"只找到 {len(found)}/3 个切片: {found}")
    if extras:
        reasons.append(f"多余的切片文件: {extras}")
    src_a = decode(source, 0)
    highlights = truth["highlights"]
    decoys = truth.get("decoys", [])
    clips, covers = [], []
    for n in names:
        p = out_dir / n
        row: dict = {"file": n, "exists": p.is_file()}
        clips.append(row)
        covers.append([False] * len(highlights))
        if not p.is_file():
            continue
        pf = probe_full(p)
        if pf is None or pf["video"] is None:
            row["error"] = "不可读或无视频流"
            continue
        v = pf["video"]
        dur = max([v["duration"]] + [a["duration"] for a in pf["audio"]])
        ratio = v["width"] / v["height"]
        row.update(duration=round(dur, 3), width=v["width"], height=v["height"], ratio=round(ratio, 4),
                   duration_ok=CLIP_DUR[0] <= dur <= CLIP_DUR[1], vertical_ok=abs(ratio - VERTICAL) <= VERTICAL_TOL,
                   has_audio=bool(pf["audio"]), loudness=loudness(p))
        sub = find_subtitles(p, pf)
        row["subtitles"] = {k: sub[k] for k in ("status", "where", "file")}
        if not pf["audio"]:
            row["error"] = "没有音频，无法回源定位"
            continue
        a = decode(p, 0)
        wins = clip_windows(a, src_a)
        row["windows"] = {"total": len(wins), "matched": sum(1 for w in wins if w["cands"]),
                          "unique": sum(1 for w in wins if len(w["cands"]) == 1)}
        clusters = cluster_offsets(wins)
        row["segments"] = [{"src_offset": c["offset"], "windows": c["n"], "src_covered": [[round(x, 2), round(y, 2)] for x, y in c["covered"]]}
                           for c in clusters]
        if not clusters:
            row["error"] = "音频在源里找不到唯一匹配（可能被重配音/混了 BGM，或背景音周期重复无法定位）"
            continue
        covered = [iv for c in clusters for iv in c["covered"]]
        row["src_start"] = round(min(x for x, _ in covered), 3)
        row["src_end"] = round(max(y for _, y in covered), 3)
        row["multi_segment"] = len(clusters) > 1
        row["foreign_audio_windows"] = sum(1 for w in wins if not w["cands"])  # 片头音效/响度处理/BGM 之类，只记录
        for k, h in enumerate(highlights):
            hl = h["end"] - h["start"]
            frac = V._union_cover(covered, h["start"] - HIGHLIGHT_TOL_S, h["end"] + HIGHLIGHT_TOL_S) / (hl + 2 * HIGHLIGHT_TOL_S)
            covers[-1][k] = frac >= HIGHLIGHT_COVER_MIN
        row["covers_highlights"] = [k for k, c in enumerate(covers[-1]) if c]
        row["lead_in_s"] = [round(highlights[k]["start"] - row["src_start"], 2) for k in row["covers_highlights"]]
        row["overlaps_decoy"] = any(V._union_cover(covered, d["start"], d["end"]) > 0 for d in decoys)
    res["clips"] = clips
    res["extras"] = extras
    ok = [c for c in clips if c.get("exists")]
    checks["durations_ok"] = len(ok) == 3 and all(c.get("duration_ok") for c in ok)
    checks["vertical_ok"] = len(ok) == 3 and all(c.get("vertical_ok") for c in ok)
    checks["all_matched_to_source"] = len(ok) == 3 and all("src_start" in c for c in ok)
    checks["distinct_highlights"] = any(
        all(covers[i][perm[i]] for i in range(3)) for perm in itertools.permutations(range(len(highlights)), 3)
    ) if len(highlights) >= 3 else False
    for c in clips:
        if c.get("exists") and not c.get("duration_ok", True):
            reasons.append(f"{c['file']} 时长 {c.get('duration')}s 不在 {CLIP_DUR}")
        if c.get("exists") and not c.get("vertical_ok", True):
            reasons.append(f"{c['file']} 画幅 {c.get('width')}x{c.get('height')} 不是 9:16")
        if c.get("error"):
            reasons.append(f"{c['file']}: {c['error']}")
        elif c.get("exists") and not c.get("covers_highlights"):
            reasons.append(f"{c['file']} 源区间 {c.get('src_start')}–{c.get('src_end')}s 没有完整覆盖任何 highlight"
                           f"{'（只碰到诱饵段）' if c.get('overlaps_decoy') else ''}"
                           f"")
    if not checks["distinct_highlights"] and all("src_start" in c for c in ok) and len(ok) == 3:
        reasons.append("3 个切片没有对应 3 个不同的 highlight")
    res["subtitles_status"] = [c.get("subtitles", {}).get("status") for c in clips]
    return finish(res, ["exactly_three_files", "durations_ok", "vertical_ok", "all_matched_to_source", "distinct_highlights"])


# ---------------------------------------------------------------- montage


def verify_montage(truth: dict, source_dir: Path, output: Path) -> dict:
    res = new_result("montage", source_dir, output)
    checks, reasons = res["checks"], res["reasons"]
    pf = base_checks(res, output)
    if pf is None:
        return res
    dur = res["output_info"]["duration"]
    checks["duration_ok"] = MONTAGE_DUR[0] <= dur <= MONTAGE_DUR[1]
    if not checks["duration_ok"]:
        reasons.append(f"时长 {dur}s 不在 {MONTAGE_DUR}")
    al = bgm_match(output, source_dir / truth["music"])
    res["bgm"] = al
    checks["bgm_present"] = al["score"] >= BGM_MONTAGE_MIN
    if not checks["bgm_present"]:
        reasons.append(f"产物音频与 BGM 互相关 {al['score']} < {BGM_MONTAGE_MIN}（BGM 没用上，或被其他声音盖住）")
    cuts = scene_cuts(output)
    checks["enough_cuts"] = len(cuts) >= CUT_MIN
    if not checks["enough_cuts"]:
        reasons.append(f"画面切点 {len(cuts)} 个 < {CUT_MIN}")
    step = 60.0 / truth["bpm"] / 2
    a_start = pf["audio"][0]["start"] - min(pf["video"]["start"], pf["audio"][0]["start"])
    rows = []
    if al["offset"] is not None:
        for c in cuts:
            bt = c - a_start + al["offset"]
            rows.append({"cut": round(c, 3), "bgm_time": round(bt, 3), "err_ms": round(grid_err(bt, step) * 1000, 1)})
    aligned = [r for r in rows if r["err_ms"] <= CUT_TOL_S * 1000]
    res["cuts"] = rows
    res["cut_aligned_frac"] = round(len(aligned) / len(rows), 3) if rows else 0.0
    checks["cuts_on_beat"] = bool(rows) and res["cut_aligned_frac"] >= CUT_ALIGNED_MIN
    if not checks["cuts_on_beat"] and rows:
        reasons.append(f"只有 {len(aligned)}/{len(rows)} 个切点落在拍点/半拍 ±{CUT_TOL_S * 1000:.0f}ms 内")
    return finish(res, ["av_drift_ok", "duration_ok", "bgm_present", "enough_cuts", "cuts_on_beat"])


# ---------------------------------------------------------------- knowledge


def verify_knowledge(truth: dict, source_dir: Path, output: Path) -> dict:
    res = new_result("knowledge", source_dir, output)
    checks, reasons = res["checks"], res["reasons"]
    pf = base_checks(res, output)
    if pf is None:
        return res
    info = res["output_info"]
    checks["landscape"] = info["width"] > info["height"]
    if not checks["landscape"]:
        reasons.append(f"不是横屏 {info['width']}x{info['height']}")
    ref = truth["reference_narration_seconds"]
    lo, hi = ref * NARRATION_RATIO[0], ref * NARRATION_RATIO[1]
    res["duration_bounds"] = [round(lo, 2), round(hi, 2)]
    checks["duration_ok"] = lo <= info["duration"] <= hi
    if not checks["duration_ok"]:
        reasons.append(f"时长 {info['duration']}s 不在参考配音时长 {ref}s 的 {NARRATION_RATIO} 倍（[{lo:.1f}, {hi:.1f}]）")
    raw = decode(output, 0, band=None)
    fl = V.SR // 20
    n = len(raw) // fl
    if n:
        rms = np.sqrt((raw[: n * fl].reshape(n, fl) ** 2).mean(axis=1) + 1e-20)
        nonsilent = float((20 * np.log10(rms) > SILENCE_DB).mean())
    else:
        nonsilent = 0.0
    res["nonsilent_frac"] = round(nonsilent, 3)
    checks["has_speech"] = nonsilent >= NONSILENT_MIN
    if not checks["has_speech"]:
        reasons.append(f"非静音占比 {nonsilent:.0%} < {NONSILENT_MIN:.0%}（没有配音，或大部分是静音）")

    frames = frames_rgb(output, 1.0)
    imgs = []
    for name in truth["images"]:
        im = image_rgb(source_dir / name)
        if im is None:
            imgs.append({"image": name, "error": "读不了素材图"})
            continue
        mse = ((frames - im) ** 2).mean(axis=(1, 2, 3)) if len(frames) else np.array([1.0])
        used = mse < IMG_MSE_USED
        imgs.append({"image": name, "best_mse": round(float(mse.min()), 5), "used": bool(used.any()),
                     "seconds_shown": int(used.sum()), "first_at_s": int(np.argmax(used)) if used.any() else None})
    res["images"] = imgs
    n_used = sum(1 for i in imgs if i.get("used"))
    res["images_used"] = n_used
    checks["images_ok"] = n_used >= IMAGES_MIN
    if not checks["images_ok"]:
        reasons.append(f"只有 {n_used} 张配图出现（要求 ≥{IMAGES_MIN}）：" + ", ".join(f"{i['image']} mse={i.get('best_mse')}" for i in imgs))

    script = " ".join(truth["sentences"])
    sub = find_subtitles(output, pf)
    ov = char_overlap(script, sub["text"]) if sub["text"] else 0.0
    status = sub["status"]
    if status == "verified" and ov < SUB_TEXT_MIN:
        status = "present_text_mismatch"
    res["subtitles"] = {"status": status, "where": sub["where"], "file": sub["file"], "script_char_overlap": round(ov, 3)}
    return finish(res, ["av_drift_ok", "landscape", "duration_ok", "has_speech", "images_ok"])


# ---------------------------------------------------------------- ytpmv


def onset_offset(tpl: np.ndarray, win: int = 80) -> float:
    """模板的能量起音点相对模板起点的偏移（秒）。"""
    env = np.sqrt(np.convolve(tpl * tpl, np.ones(win) / win, mode="same"))
    return float(np.argmax(env >= ONSET_FRAC * env.max())) / V.SR if env.max() > 0 else 0.0


def find_occurrences(tpl: np.ndarray, out_a: np.ndarray, thr: float) -> list[tuple[float, float]]:
    """tpl 在 out_a 里所有互相关 ≥thr 的出现（贪心非极大值抑制），返回 [(起点秒, 分数)]。"""
    if len(out_a) < len(tpl) or float(tpl @ tpl) < 1e-12:
        return []
    curve = V.ncc_curve(tpl, out_a).copy()
    half = max(len(tpl) // 2, 1)
    hits = []
    while len(hits) < 64:
        k = int(np.argmax(curve))
        if curve[k] < thr:
            break
        hits.append((k / V.SR, float(curve[k])))
        curve[max(0, k - half): k + half] = 0.0
    return hits


def verify_ytpmv(truth: dict, source_dir: Path, output: Path) -> dict:
    res = new_result("ytpmv", source_dir, output)
    checks, reasons = res["checks"], res["reasons"]
    pf = base_checks(res, output)
    if pf is None:
        return res
    al = bgm_match(output, source_dir / truth["music"])
    res["bgm"] = al
    checks["bgm_present"] = al["score"] >= BGM_YTPMV_MIN
    if not checks["bgm_present"]:
        reasons.append(f"产物音频与 BGM 互相关 {al['score']} < {BGM_YTPMV_MIN}")

    src_a = decode(source_dir / truth["source"], 0)
    out_a = decode(output, 0)
    cands, onsets = [], []
    for i, s in enumerate(truth["syllables"]):
        tpl = src_a[int(round(s["start"] * V.SR)): int(round(s["end"] * V.SR))]
        onsets.append(onset_offset(tpl))
        cands += [{"syllable": i, "text": s["text"], "pos": round(p, 4), "score": round(sc, 3)}
                  for p, sc in find_occurrences(tpl, out_a, SYL_MIN)]
    cands.sort(key=lambda c: -c["score"])
    hits: list[dict] = []
    for c in cands:  # 跨音节的位置 NMS：同一处只算分最高的那个音节
        if all(abs(c["pos"] - h["pos"]) >= SYL_NMS_S for h in hits):
            hits.append(c)
    hits.sort(key=lambda h: h["pos"])
    step = 60.0 / truth["bpm"] / 2
    off = al["offset"]
    for h in hits:
        h["err_ms"] = None if off is None else round(grid_err(h["pos"] + off, step) * 1000, 1)
        h["onset_err_ms"] = None if off is None else round(grid_err(h["pos"] + onsets[h["syllable"]] + off, step) * 1000, 1)
    res["occurrences"] = hits
    res["occurrence_count"] = len(hits)
    res["distinct_syllables"] = len({h["syllable"] for h in hits})
    tol = SYL_TOL_S * 1000
    frac = lambda key: round(sum(1 for h in hits if h[key] is not None and h[key] <= tol) / len(hits), 3) if hits else 0.0  # noqa: E731
    res["aligned_frac_start"], res["aligned_frac_onset"] = frac("err_ms"), frac("onset_err_ms")
    res["aligned_frac"] = max(res["aligned_frac_start"], res["aligned_frac_onset"])  # 音节起点对拍 / 能量起音对拍，取高者
    checks["enough_occurrences"] = len(hits) >= SYL_COUNT_MIN
    checks["has_repeats"] = len(hits) > res["distinct_syllables"]  # 补充规格：原片放一遍也有 12 次，必须有音节被重复
    checks["on_beat"] = bool(hits) and res["aligned_frac"] >= SYL_ALIGNED_MIN
    if not checks["enough_occurrences"]:
        reasons.append(f"台词音节在产物里只出现 {len(hits)} 次 < {SYL_COUNT_MIN}")
    if not checks["has_repeats"]:
        reasons.append(f"没有音节被重复（{len(hits)} 次出现 / {res['distinct_syllables']} 个不同音节）")
    if hits and not checks["on_beat"]:
        reasons.append(f"音节对拍比例 {res['aligned_frac']:.0%}（起点 {res['aligned_frac_start']:.0%} / 能量起音 {res['aligned_frac_onset']:.0%}）"
                       f" < {SYL_ALIGNED_MIN:.0%}（±{SYL_TOL_S * 1000:.0f}ms 内，拍点/半拍）")
    return finish(res, ["av_drift_ok", "bgm_present", "enough_occurrences", "has_repeats", "on_beat"])


# ---------------------------------------------------------------- entry


def verify(truth_path: Path, source: Path, output: Path, task: str) -> dict:
    truth = json.loads(truth_path.read_text())
    fn = {"game_commentary": verify_game, "stream_clips": verify_clips, "montage": verify_montage,
          "knowledge": verify_knowledge, "ytpmv": verify_ytpmv}[task]
    res = fn(truth, source, output)
    res["pass"] = bool(res["pass"])
    return res


def human(res: dict) -> str:
    L = [f"[{res['task']}] {'PASS' if res['pass'] else 'FAIL'}  output={res['output']}"]
    skip = {"task", "source", "output", "checks", "reasons", "pass"}
    for k, v in res.items():
        if k in skip:
            continue
        s = json.dumps(v, ensure_ascii=False)
        L.append(f"  {k}: {s if len(s) <= 400 else s[:400] + '…'}")
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
    if not a.truth.is_file():
        print(f"usage error: --truth 文件不存在: {a.truth}", file=sys.stderr)
        return 2
    if not a.source.exists():
        print(f"usage error: --source 不存在: {a.source}", file=sys.stderr)
        return 2
    res = verify(a.truth, a.source, a.output, a.task)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else human(res))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
