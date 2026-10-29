"""语音 / 静音检测：自适应阈值 + 迟滞 + 字间停顿合并。

不依赖固定 dBFS 阈值——底噪 -38 dBFS 的粉噪录音和 -52 dBFS 的干净录音用同一套参数。
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from autocut.audio import DB_FLOOR, frame_db

log = logging.getLogger(__name__)

_HOP = 0.01  # 帧移（秒）；帧窗 25ms
_WIN = 0.025
_NOISE_PCT = 10  # 帧 dB 的低分位 → 底噪
_SPEECH_PCT = 95  # 高分位 → 语音电平
_MAX_RANGE_DB = 50.0  # 底噪下限不低于 语音电平-50dB：数字静音（-100）下，阈值不能被拉到微弱拖尾的水平
_MIN_RANGE_DB = 8.0  # 语音电平与底噪的差小于此值 → 分布里没有可分的两类（纯噪声/单一电平），判无语音
_MIN_MARGIN_DB = 6.0  # 阈值至少比底噪高这么多
_RANGE_FRAC = 0.5  # 阈值 = 底噪 + 此比例 × (语音电平-底噪)（dB 域中点），再不小于 _MIN_MARGIN_DB
_LOW_FRAC = 0.15  # 迟滞低阈值 = 底噪 + 此比例 × 跨度，至少比底噪高 _LOW_MARGIN_DB
_LOW_MARGIN_DB = 4.0
_LOW_HYST_DB = 6.0  # 手动指定阈值时，低阈值 = 阈值 - 此值


@dataclass(frozen=True)
class SpeechResult:
    regions: tuple[tuple[float, float], ...]  # 语音区间（秒），升序、互不相交
    noise_floor_db: float  # 估计的底噪（帧 dB 的低分位）
    speech_level_db: float  # 估计的语音电平（帧 dB 的高分位）
    threshold_db: float  # 实际使用的（高）阈值
    frame_hop: float  # 帧移（秒），即 dB 曲线的时间分辨率


def invert_regions(regions: Sequence[tuple[float, float]], total: float) -> list[tuple[float, float]]:
    """区间的补集：把语音区间变成 [0, total] 内的静音区间。"""
    out, cur = [], 0.0
    for s, e in regions:
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < total:
        out.append((cur, total))
    return out


def _hysteresis_runs(db: np.ndarray, high: float, low: float) -> list[tuple[int, int]]:
    """帧索引区间 [a, b)：由 db>=low 的连续段构成，且段内至少有一帧 db>=high。"""
    lo = np.concatenate([[False], db >= low, [False]])
    edges = np.flatnonzero(lo[1:] != lo[:-1])
    starts, ends = edges[0::2], edges[1::2]  # lo 为真的连续段 [start, end)
    if len(starts) == 0:
        return []
    hi_cum = np.concatenate([[0], np.cumsum(db >= high)])
    keep = hi_cum[ends] - hi_cum[starts] > 0
    return list(zip(starts[keep].tolist(), ends[keep].tolist()))


def _merge(regions: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    """相邻区间的间隔 < max_gap 就并起来（重叠必并）。"""
    out: list[list[float]] = []
    for s, e in regions:
        if out and s - out[-1][1] < max_gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def detect_speech(
    x: np.ndarray,
    sr: int,
    *,
    threshold_db: float | None = None,
    min_silence: float = 0.4,
    min_speech: float = 0.08,
    pad: float = 0.0,
) -> SpeechResult:
    """在单声道信号 x 上检测语音区间。

    阈值：帧 dB 分布的低分位当底噪、高分位当语音电平，阈值取两者之间（且至少比底噪高 6 dB）。
    迟滞：高于阈值的帧作种子，向两侧延伸到低阈值，保住字头字尾的弱辅音。
    min_silence：短于它的停顿并入语音（字间停顿）；min_speech：短于它的语音段丢弃（爆音/咔哒）；
    pad：每个区间两侧各外扩的秒数（重叠则合并）。threshold_db 非 None 时跳过自适应估计。
    """
    t0 = time.perf_counter()
    db = frame_db(x, sr, _HOP, _WIN)
    total = len(x) / sr
    if db.size == 0:
        return SpeechResult((), DB_FLOOR, DB_FLOOR, DB_FLOOR, _HOP)

    noise = float(np.percentile(db, _NOISE_PCT))
    speech = float(np.percentile(db, _SPEECH_PCT))
    noise_eff = max(noise, speech - _MAX_RANGE_DB)
    span = speech - noise_eff

    if threshold_db is not None:
        high, low, separable = float(threshold_db), float(threshold_db) - _LOW_HYST_DB, True
    else:
        high = noise_eff + max(_MIN_MARGIN_DB, _RANGE_FRAC * span)
        low = noise_eff + max(_LOW_MARGIN_DB, _LOW_FRAC * span)
        separable = span >= _MIN_RANGE_DB
        if not separable:
            log.warning("帧 dB 分布里没有可分的两类（语音电平 %.1f - 底噪 %.1f = %.1f dB < %.0f），判为无语音",
                        speech, noise, speech - noise, _MIN_RANGE_DB)

    runs = _hysteresis_runs(db, high, low) if separable else []
    # 帧 i 覆盖 [(i-0.5)hop, (i+0.5)hop)
    regions = [(max(0.0, (a - 0.5) * _HOP), min(total, (b - 0.5) * _HOP)) for a, b in runs]
    n_raw = len(regions)
    regions = _merge(regions, min_silence)
    n_merged = len(regions)
    regions = [(s, e) for s, e in regions if e - s >= min_speech]
    if pad > 0:
        regions = _merge([(max(0.0, s - pad), min(total, e + pad)) for s, e in regions], 0.0)

    log.info(
        "detect_speech: 底噪 %.1f dB, 语音电平 %.1f dB, 阈值 %.1f/%.1f dB → %d 段(原始 %d, 并停顿后 %d), 语音 %.1fs/%.1fs, %.0fms",
        noise, speech, high, low, len(regions), n_raw, n_merged, sum(e - s for s, e in regions), total,
        (time.perf_counter() - t0) * 1e3,
    )
    return SpeechResult(tuple(regions), noise, speech, high, _HOP)

