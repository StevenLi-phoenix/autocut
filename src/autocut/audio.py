"""音频解码与帧级电平：ffmpeg 管道解码为 float32 单声道，numpy 算 RMS。"""

import logging
import subprocess
import time
from pathlib import Path

import numpy as np

from autocut.probe import MediaError, probe, probe_failure

log = logging.getLogger(__name__)

DB_FLOOR = -100.0  # 静音下限（dBFS）
_MS_FLOOR = 10 ** (DB_FLOOR / 10)


def resolve_track(path: str | Path, track: int | str | None) -> int:
    """音轨序号（0 起，按音轨顺序）。None → 0；int 校验范围；str 按 title 匹配（大小写不敏感）。"""
    if track is None:
        return 0
    path = Path(path)
    info = probe(path)
    n = len(info.audio)
    if n == 0:
        raise MediaError(f"{path} 没有音频流，无法选择音轨 {track!r}")
    titles = [a.title for a in info.audio]
    if isinstance(track, int):
        if not 0 <= track < n:
            raise MediaError(f"{path} 的 track={track} 越界：该文件有 {n} 条音轨（title：{titles}），序号范围 0..{n - 1}")
        return track
    wanted = track.strip().casefold()
    for a in info.audio:
        if a.title and a.title.strip().casefold() == wanted:
            return a.track_number
    raise MediaError(f"{path} 里没有 title 为 {track!r} 的音轨；现有音轨 title：{titles}")


def load_audio(
    path: str | Path,
    track: int | str | None = None,
    sr: int = 16000,
    start: float | None = None,
    end: float | None = None,
) -> np.ndarray:
    """解码为 float32 单声道（[-1, 1]），重采样到 sr。

    track：音轨序号或 title（如 "mic"），None 取第 0 条；start/end：秒，只解码这一段。
    直接读 ffmpeg 管道，不写临时文件。失败抛 MediaError。
    """
    path = Path(path)
    idx = resolve_track(path, track)
    if start is not None and start < 0:
        raise ValueError(f"start 不能为负：{start}")
    if end is not None and end <= (start or 0.0):
        raise ValueError(f"end({end}) 必须大于 start({start or 0.0})")

    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.6f}"]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0.0):.6f}"]
    cmd += ["-i", str(path), "-map", f"0:a:{idx}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]

    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError as e:
        raise MediaError("找不到 ffmpeg：请安装 ffmpeg（macOS: brew install ffmpeg）并确认它在 PATH 里") from e
    stderr = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0:
        if "matches no streams" in stderr or "does not contain any stream" in stderr:
            raise MediaError(f"{path} 没有音频流（track={track!r}）：这是纯视频文件，或音轨序号超出范围")
        raise probe_failure(path, stderr, tool="ffmpeg")
    x = np.frombuffer(r.stdout, dtype="<f4").astype(np.float32)  # astype 复制成可写数组
    log.debug(
        "load_audio %s track=%s(a:%d) sr=%d [%s,%s] → %d 样本 %.2fs，耗时 %.0fms",
        path.name, track, idx, sr, start, end, x.size, x.size / sr, (time.perf_counter() - t0) * 1e3,
    )
    return x


def windowed_ms(x: np.ndarray, sr: int, hop: float, win: float) -> np.ndarray:
    """滑窗均方（float64）。第 i 帧以 i*hop 为中心、宽 win；超出信号的部分不计入分母，首尾不被补零拉低。

    前缀和实现，O(N)，与窗口宽度无关——150s 的音频在毫秒级。
    """
    n = len(x)
    if n == 0:
        return np.zeros(0)
    half = max(1, int(round(win * sr / 2)))
    step = max(1, int(round(hop * sr)))
    centers = np.arange(n // step + 1) * step
    lo = np.clip(centers - half, 0, n)
    hi = np.clip(centers + half, 0, n)
    cs = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    return np.maximum(cs[hi] - cs[lo], 0.0) / np.maximum(hi - lo, 1)


def frame_db(x: np.ndarray, sr: int, hop: float = 0.01, win: float = 0.025) -> np.ndarray:
    """逐帧 RMS 电平（dBFS，float32），静音下限 -100。第 i 帧以 i*hop 秒为中心。"""
    ms = windowed_ms(x, sr, hop, win)
    return (10.0 * np.log10(np.maximum(ms, _MS_FLOOR))).astype(np.float32)
