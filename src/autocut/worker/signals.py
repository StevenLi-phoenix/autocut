"""确定性的"眼睛"：关键帧拼图（contact sheet）、响度曲线、场景切换点、OCR 文字。没有任何 LLM 调用。

画面理解由 DSH agent（唯一的大脑）自己做：它用 read_image 看拼图，或用 `ar frame` 按时间点取帧。
brief 里放的是这些信号和拼图路径，不放任何"别的模型对画面的描述"。
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)
FONT = "/System/Library/Fonts/Helvetica.ttc"
CELL_W = 320


def grab(src: Path, t: float, dst: Path, width: int = CELL_W, label: str | None = None) -> Path:
    """取一帧存成 jpg（有些素材是 yuv444 / 受限范围，mjpeg 要 yuvj420p）；label 画在左上角。"""
    vf = f"scale={width}:-2:out_range=pc"
    if label:
        txt = label.replace(":", "\\:").replace("'", "")
        vf += f",drawtext=fontfile={FONT}:text='{txt}':x=6:y=6:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=4"
    vf += ",format=yuvj420p"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "4", str(dst)]
    r = subprocess.run(cmd, capture_output=True, timeout=90)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"抽帧失败 t={t:.2f}s {src.name}：{r.stderr.decode('utf-8', 'replace')[-200:]}")
    return dst


def contact_sheet(cells: list[tuple[Path, float, str]], out_dir: Path, name: str, cols: int = 4) -> Path:
    """cells = [(视频, 时间, 标签)] → 一张带标签的拼图。返回 jpg 路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = []
    for k, (src, t, label) in enumerate(cells):
        tiles.append(grab(src, t, out_dir / f".{name}-{k:02d}.jpg", label=label))
    rows = -(-len(tiles) // cols)
    dst = out_dir / f"{name}.jpg"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1", "-i", str(out_dir / f".{name}-%02d.jpg"), "-vf", f"tile={cols}x{rows}:padding=4:color=black", "-frames:v", "1", "-q:v", "4", str(dst)]
    r = subprocess.run(cmd, capture_output=True, timeout=90)
    for p in tiles:
        p.unlink(missing_ok=True)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"拼图失败 {name}：{r.stderr.decode('utf-8', 'replace')[-200:]}")
    return dst


def loudness_curve(x: np.ndarray, sr: int, step: float = 5.0) -> list[float]:
    """每 step 秒一个 RMS(dB)，给 agent 看哪里安静、哪里激烈。"""
    n = int(step * sr)
    out = []
    for i in range(0, max(len(x), 1), n):
        seg = x[i:i + n]
        out.append(round(float(20 * np.log10(np.sqrt(np.mean(seg.astype(np.float64) ** 2)) + 1e-9)), 1) if len(seg) else -90.0)
    return out


def ocr(images: list[Path], binary: str | None) -> dict[str, list[str]]:
    """macOS Vision OCR（复用烤肉的 vision_ocr，已编译的二进制）；没配置或失败返回空，不影响流程。"""
    if not binary or not images:
        return {}
    try:
        r = subprocess.run([binary], input="\n".join(str(p) for p in images), capture_output=True, text=True, timeout=300)
        out: dict[str, list[str]] = {}
        for line in r.stdout.splitlines():
            d = json.loads(line)
            texts = [i["text"] for i in d.get("items", []) if float(i.get("conf", 0)) >= 0.5 and i["text"].strip()]
            if texts:
                out[d["path"]] = texts[:6]
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("OCR 失败（brief 不带 OCR 文字）：%s", e)
        return {}


def frames_brief(src: Path, times: list[float], out_dir: Path, name: str, *, per_sheet: int = 12, ocr_bin: str | None = None,
                 map_time: Any = None) -> list[dict[str, Any]]:
    """按时间点取关键帧，每 per_sheet 张拼一张图。返回 [{sheets 路径, cells:[{i, t, label}], ocr}]。
    map_time(t)->(源文件, 源内时间)：成片时间轴 → 素材时间；缺省时 t 就是素材时间。"""
    sheets = []
    for b in range(0, len(times), per_sheet):
        chunk = times[b:b + per_sheet]
        cells = []
        meta = []
        for k, t in enumerate(chunk):
            s, st = map_time(t) if map_time else (src, t)
            label = f"{b + k + 1} {int(t) // 60:02d}:{int(t) % 60:02d}"
            cells.append((s, st, label))
            meta.append({"i": b + k + 1, "t": round(t, 1), "label": label})
        p = contact_sheet(cells, out_dir, f"{name}-sheet{b // per_sheet + 1}")
        sheets.append({"path": str(p), "cells": meta})
    if ocr_bin:  # OCR 对单帧做，不对拼图做
        singles = []
        for b in range(0, len(times), per_sheet):
            for k, t in enumerate(times[b:b + per_sheet]):
                s, st = map_time(t) if map_time else (src, t)
                singles.append(grab(s, st, out_dir / f".ocr-{b + k:03d}.jpg", width=960))
        res = ocr(singles, ocr_bin)
        for sh in sheets:
            for c in sh["cells"]:
                texts = res.get(str(out_dir / f".ocr-{c['i'] - 1:03d}.jpg"))
                if texts:
                    c["ocr"] = texts
        for p in singles:
            p.unlink(missing_ok=True)
    return sheets
