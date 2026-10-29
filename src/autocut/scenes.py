"""镜头切点检测：缩小灰度帧的像素差 + 直方图差，阈值按整段视频的分布自适应。"""

import logging
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from autocut.probe import MediaError, probe, probe_failure

log = logging.getLogger(__name__)

_HIST_BINS = 32
_CHUNK_FRAMES = 512  # 每次从管道读多少帧
_MAD_K = 8.0  # 阈值 = 中位数 + K·1.4826·MAD
_ABS_MIN = 0.12  # 阈值绝对下限：画面静止时 MAD=0，不能因此每帧都报切点
_MIN_GAP_FRAMES = 3  # 相邻切点至少隔这么多帧；更近的（如单帧闪白的"进+出"）只留得分高的
_CONTEXT_RATIO = 1.8  # 切点得分必须高过其 ±_CONTEXT 帧内其他帧最大得分的这个倍数，滤掉持续的剧烈运动
_CONTEXT = 6


def frame_scores(frames: np.ndarray, prev: np.ndarray | None = None) -> np.ndarray:
    """每帧相对前一帧的变化量 ∈ [0, 1]：(平均绝对像素差/255 + 直方图 L1 差/2) / 2。

    frames：(N, H, W) uint8；prev：前一块的最后一帧（(H, W)），分块读取时用来衔接。无前驱的第 0 帧得 0。
    像素差抓画面内容的变化，直方图差对平移/缩放不敏感——两者取平均，运动不会被当成切换。
    """
    n = len(frames)
    scores = np.zeros(n)
    if n == 0:
        return scores
    f = frames.astype(np.int16)
    seq = f if prev is None else np.concatenate([prev[None].astype(np.int16), f])
    mad = np.abs(np.diff(seq, axis=0)).mean(axis=(1, 2)) / 255.0
    bins = (seq >> 3).reshape(len(seq), -1)  # 256 灰阶 → 32 档
    hist = np.stack([np.bincount(b, minlength=_HIST_BINS) for b in bins]).astype(np.float64)
    hist /= hist.sum(axis=1, keepdims=True)
    hdiff = 0.5 * np.abs(np.diff(hist, axis=0)).sum(axis=1)
    scores[n - len(mad) :] = (mad + hdiff) / 2
    return scores


def pick_cut_frames(scores: np.ndarray, threshold: float | None = None) -> tuple[list[int], float]:
    """从逐帧得分里选切点帧（新镜头的第一帧）索引，返回 (索引列表, 实际阈值)。

    threshold=None 时按分布自适应：中位数 + K·1.4826·MAD，不低于绝对下限。
    入选条件：超过阈值、是 ±_MIN_GAP_FRAMES 内的最大值、且明显高于附近其他帧（持续运动不算切换）。
    """
    n = len(scores)
    s = np.asarray(scores, dtype=np.float64).copy()
    if n:
        s[0] = 0.0  # 第 0 帧没有前驱
    if threshold is None:
        body = s[1:] if n > 1 else s
        med = float(np.median(body)) if body.size else 0.0
        mad = float(np.median(np.abs(body - med))) if body.size else 0.0
        thr = max(med + _MAD_K * 1.4826 * mad, _ABS_MIN)
    else:
        thr = float(threshold)
    if n < 2:
        return [], thr

    # 首尾短于 _MIN_GAP_FRAMES 帧的"镜头"不成立（如动画源第 1 帧的初始化跳变），不报
    s[:_MIN_GAP_FRAMES] = 0.0
    s[max(n - _MIN_GAP_FRAMES + 1, 0) :] = 0.0
    cand = np.flatnonzero(s > thr)
    picked: list[int] = []
    for i in cand[np.argsort(-s[cand])]:  # 得分从高到低：高分的先占位，近邻的低分点被压制
        if any(abs(int(i) - j) < _MIN_GAP_FRAMES for j in picked):
            continue
        picked.append(int(i))
    out = []
    for i in sorted(picked):
        lo, hi = max(0, i - _CONTEXT), min(n, i + _CONTEXT + 1)
        # 只和更远的邻居比：_MIN_GAP_FRAMES 内的点可能是同一事件的余波
        far = np.concatenate([s[lo : max(lo, i - _MIN_GAP_FRAMES + 1)], s[min(hi, i + _MIN_GAP_FRAMES) : hi]])
        ref = float(far.max()) if far.size else 0.0
        if threshold is None and s[i] < _CONTEXT_RATIO * ref:
            log.debug("丢弃第 %d 帧：得分 %.3f 没有明显高于附近(%.3f)，像持续运动", i, s[i], ref)
            continue
        out.append(i)
    return out, thr


# ------------------------------------------------------------------ 视频读取


def _video_pts(path: Path) -> np.ndarray:
    """视频流每个包的 pts（秒，升序）。读容器索引，不解码，VFR 也准。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise probe_failure(path, r.stderr)
    vals = []
    for tok in r.stdout.split():
        try:
            vals.append(float(tok.rstrip(",")))
        except ValueError:
            continue
    return np.sort(np.asarray(vals))


def _read_gray_frames(path: Path, scale: tuple[int, int]) -> Iterator[np.ndarray]:
    """ffmpeg 输出缩小的灰度 rawvideo，分块（最多 _CHUNK_FRAMES 帧）yield (n, H, W) uint8。"""
    w, h = scale
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an",
        "-vf", f"scale={w}:{h}:flags=area,format=gray", "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise MediaError("找不到 ffmpeg：请安装 ffmpeg（macOS: brew install ffmpeg）并确认它在 PATH 里") from e
    assert proc.stdout is not None and proc.stderr is not None
    size, chunk_bytes = w * h, w * h * _CHUNK_FRAMES
    try:
        while True:
            buf = proc.stdout.read(chunk_bytes)  # BufferedReader.read(n) 读满 n 字节或到 EOF 才返回
            n = len(buf) // size
            if n:
                yield np.frombuffer(buf, dtype=np.uint8, count=n * size).reshape(n, h, w)
            if len(buf) < chunk_bytes:
                break
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0:
        raise probe_failure(path, err, tool="ffmpeg")


def detect_cuts(path: str | Path, *, threshold: float | None = None, scale: tuple[int, int] = (64, 36)) -> list[float]:
    """返回镜头切点的时间（秒）：新镜头第一帧的 pts。

    scale：分析用的缩略尺寸；threshold：手动阈值（得分 ∈ [0,1]），None 则自适应。
    无视频流 / 文件损坏抛 MediaError。
    """
    path = Path(path)
    t0 = time.perf_counter()
    info = probe(path)
    if info.video is None:
        raise MediaError(f"{path} 没有视频流，无法检测镜头切点（可能是纯音频文件）")

    scores_parts: list[np.ndarray] = []
    prev: np.ndarray | None = None
    for chunk in _read_gray_frames(path, scale):
        scores_parts.append(frame_scores(chunk, prev))
        prev = chunk[-1]
    scores = np.concatenate(scores_parts) if scores_parts else np.zeros(0)

    pts = _video_pts(path)
    if len(pts) != len(scores):
        fps = info.video.fps or 30.0
        log.warning("%s: 包数 %d 与解码帧数 %d 不一致，pts 退回 index/fps(%.3f)", path.name, len(pts), len(scores), fps)
        pts = np.arange(len(scores)) / fps

    idx, thr = pick_cut_frames(scores, threshold)
    cuts = [float(pts[i]) for i in idx]
    log.info(
        "detect_cuts %s: %d 帧, 阈值 %.3f(%s), 切点 %d 个 %s, %.0fms",
        path.name, len(scores), thr, "手动" if threshold is not None else "自适应", len(cuts),
        [round(c, 3) for c in cuts[:10]], (time.perf_counter() - t0) * 1e3,
    )
    return cuts
