"""响度：ffmpeg ebur128 的积分响度，以及供高光检测用的简化短时响度曲线。"""

import logging
import re
import subprocess
import time
from pathlib import Path

import numpy as np

from autocut.audio import resolve_track, windowed_ms
from autocut.probe import MediaError, probe_failure

log = logging.getLogger(__name__)

_LOUDNESS_OFFSET = -0.691  # BS.1770：LKFS = -0.691 + 10·log10(加权均方)
_MS_FLOOR = 1e-10
_I_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?inf|-?\d+(?:\.\d+)?)\s*LUFS", re.IGNORECASE)


def integrated_lufs(path: str | Path, track: int | str | None = None) -> float:
    """整段的积分响度（LUFS，BS.1770 / EBU R128，含门限）。track 同 load_audio。

    纯静音时 ffmpeg 报 -inf，这里原样返回 float("-inf")。
    """
    path = Path(path)
    idx = resolve_track(path, track)
    cmd = [
        "ffmpeg", "-nostdin", "-nostats", "-hide_banner", "-v", "info", "-i", str(path),
        "-map", f"0:a:{idx}", "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-",
    ]
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise MediaError("找不到 ffmpeg：请安装 ffmpeg（macOS: brew install ffmpeg）并确认它在 PATH 里") from e
    if r.returncode != 0:
        if "matches no streams" in r.stderr:
            raise MediaError(f"{path} 没有音频流（track={track!r}），无法测响度")
        raise probe_failure(path, r.stderr, tool="ffmpeg")
    m = _I_RE.findall(r.stderr)
    if not m:
        raise MediaError(f"ffmpeg 没有为 {path} 输出 ebur128 的 Summary（可能是 ffmpeg 版本不支持 ebur128 滤镜）：{r.stderr[-300:]!r}")
    lufs = float(m[-1])
    log.debug("integrated_lufs %s track=%s → %.2f LUFS (%.0fms)", path.name, track, lufs, (time.perf_counter() - t0) * 1e3)
    return lufs


# ------------------------------------------------------------------ K 加权（频域实现）


def _k_weighting_biquads(sr: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """BS.1770 K 加权的两级 biquad (b, a)：高频搁架（+4 dB@>2kHz）与 RLB 高通（~38Hz）。

    用 libebur128 / ffmpeg 的双线性变换推导，对任意采样率成立；48 kHz 下与标准表里的系数一致。
    """
    # 第一级：高频搁架
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / sr)
    vh = 10 ** (gain_db / 20)
    vb = vh**0.4996667741545416
    a0 = 1.0 + k / q + k * k
    shelf = (
        np.array([(vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0]),
        np.array([1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]),
    )
    # 第二级：RLB 高通
    f0, q = 38.13547087602444, 0.5003270373238773
    k = np.tan(np.pi * f0 / sr)
    a0 = 1.0 + k / q + k * k
    rlb = (np.array([1.0, -2.0, 1.0]), np.array([1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]))
    return [shelf, rlb]


def k_weighting_gain(freqs: np.ndarray, sr: int) -> np.ndarray:
    """K 加权在各频率处的幅度增益（线性）。"""
    z = np.exp(-1j * 2 * np.pi * freqs / sr)
    g = np.ones_like(freqs, dtype=np.float64)
    for b, a in _k_weighting_biquads(sr):
        g *= np.abs((b[0] + b[1] * z + b[2] * z**2) / (a[0] + a[1] * z + a[2] * z**2))
    return g


def short_term_curve(x: np.ndarray, sr: int, window: float = 3.0, hop: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """简化的短时响度：K 加权后在 window 秒的滑窗上取均方，LUFS 刻度。

    不做门限；K 加权用 FFT 乘幅度响应（零相位，只影响功率，不需要时域 IIR 循环，纯 numpy 也很快）。
    返回 (times, db)：times[i] = i*hop，是窗口**中心**时刻，所以峰值时刻直接就是"最响的那一段的中点"。
    """
    n = len(x)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    spec = np.fft.rfft(x.astype(np.float64))
    spec *= k_weighting_gain(np.fft.rfftfreq(n, 1.0 / sr), sr)
    xk = np.fft.irfft(spec, n)
    ms = windowed_ms(xk, sr, hop, window)
    db = _LOUDNESS_OFFSET + 10.0 * np.log10(np.maximum(ms, _MS_FLOOR))
    return np.arange(len(db)) * hop, db.astype(np.float32)


def top_peaks(times: np.ndarray, db: np.ndarray, n: int = 3, min_distance: float = 20.0) -> list[tuple[float, float]]:
    """曲线上最高的 n 个局部峰，非极大值抑制：任意两个入选峰相距 ≥ min_distance 秒。

    返回 [(时刻, 值), ...]，按值降序；不足 n 个就有几个返回几个。
    """
    if len(db) == 0:
        return []
    left = np.concatenate([[-np.inf], db[:-1]])
    right = np.concatenate([db[1:], [-np.inf]])
    cand = np.flatnonzero((db > left) & (db >= right))  # 平台只取第一个点
    picked: list[int] = []
    for i in cand[np.argsort(-db[cand], kind="stable")]:
        if all(abs(times[i] - times[j]) >= min_distance for j in picked):
            picked.append(int(i))
            if len(picked) == n:
                break
    return [(float(times[i]), float(db[i])) for i in picked]
