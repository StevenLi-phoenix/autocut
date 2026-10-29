"""节拍检测：谱通量 onset 强度 → 自相关 tempo → Ellis 2007 动态规划跟拍 → downbeat。

只用 numpy。为卡点混剪服务：要的是"每一拍在哪、哪一拍是小节首拍"，精度目标 ±25ms。
"""

import logging
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

_MIN_BPM, _MAX_BPM = 60.0, 200.0
_PRIOR_BPM = 120.0
_PRIOR_SIGMA_OCT = 1.5  # 先验宽度（八度）。很弱：60 BPM 处权重仍有 0.8，只用来打破倍频/半频的平局
_TIGHTNESS = 100.0  # Ellis 2007 的 α：越大越要求拍间隔贴近 tempo
_HOP_S = 0.005  # onset 包络的时间分辨率
_FFT_S = 0.011  # STFT 窗长（秒，取 2 的幂个样本）。窗越短，谱通量峰值相对真实起音的提前量越小
_LOG_GAMMA = 1e3  # log1p(γ·|X|) 的压缩系数：峰值归一化后可分辨到约 -80 dBFS
_MIN_DURATION_S = 2.0  # 短于此无法估 tempo
_LOW_BAND_HZ = (30.0, 150.0)  # 底鼓能量所在频带
_BASS_BAND_HZ = (30.0, 300.0)  # 小节首拍常伴随低音根音变化
_ACCENT_TAIL_FRAC = 0.45  # 取每拍后 45%（底鼓/军鼓已衰减，只剩持续音）比较低音音高的变化


@dataclass(frozen=True)
class BeatResult:
    bpm: float
    beats: tuple[float, ...]  # 每一拍的时刻（秒）
    downbeats: tuple[float, ...]  # 小节首拍（beats 的子集，每 4 拍一个，4/4 拍假设）
    confidence: float  # 0..1，onset 包络的周期性强度；纯噪声/静音接近 0


_EMPTY = BeatResult(0.0, (), (), 0.0)


# ------------------------------------------------------------------ onset 强度


def _stft_mag(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """汉宁窗 STFT 幅度，center=True（两端各补 n_fft//2 个反射样本）。形状 (帧, 频点)。"""
    pad = n_fft // 2
    xp = np.pad(x, (pad, pad), mode="reflect")
    n_frames = 1 + (len(xp) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    win = np.hanning(n_fft + 1)[:-1].astype(np.float32)
    return np.abs(np.fft.rfft(xp[idx] * win, axis=1))


def onset_strength(x: np.ndarray, sr: int) -> tuple[np.ndarray, int, int]:
    """log 幅度谱的谱通量（半波整流后各频点求平均），再减去 ~0.4s 的滑动均值做高通、除以标准差。

    返回 (包络, hop 样本数, n_fft)；包络第 t 帧对应时刻 t*hop/sr。
    """
    n_fft = 1 << int(np.ceil(np.log2(_FFT_S * sr)))
    hop = max(1, int(round(_HOP_S * sr)))
    mag = _stft_mag(x, n_fft, hop)
    logmag = np.log1p(_LOG_GAMMA * mag)
    flux = np.maximum(np.diff(logmag, axis=0, prepend=np.zeros((1, logmag.shape[1]), logmag.dtype)), 0.0).mean(axis=1)
    w = max(3, int(round(0.4 / (hop / sr))))
    local_mean = np.convolve(flux, np.ones(w) / w, mode="same")
    env = np.maximum(flux - local_mean, 0.0)
    sd = float(env.std())
    return (env / sd if sd > 1e-9 else env), hop, n_fft


# ------------------------------------------------------------------ tempo


def _autocorr(env: np.ndarray, max_lag: int) -> np.ndarray:
    """去均值后的无偏归一化自相关（lag=0 处为 1）。"""
    e = env - env.mean()
    n = len(e)
    f = np.fft.rfft(e, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[: max_lag + 1]
    ac = ac / (np.arange(n, n - max_lag - 1, -1))  # 无偏：每个 lag 的重叠样本数不同
    return ac / (ac[0] + 1e-12)


def estimate_tempo(env: np.ndarray, frame_s: float) -> tuple[float, float]:
    """onset 包络的自相关 → (bpm, 周期性强度)。

    候选 lag 对应 60–200 BPM；自相关先叠加 2 倍、4 倍 lag 处的值（真拍长的整数倍也应当有峰，
    这样半频不会赢过正确的拍长），再乘以以 120 BPM 为中心的对数正态先验，取最大者并做抛物线插值。
    """
    lo, hi = int(np.floor(60.0 / _MAX_BPM / frame_s)), int(np.ceil(60.0 / _MIN_BPM / frame_s))
    ac = _autocorr(env, min(4 * hi, len(env) - 1))
    lags = np.arange(lo, hi + 1)

    def at(mult: int) -> np.ndarray:
        idx = np.minimum(lags * mult, len(ac) - 1)
        return np.where(lags * mult < len(ac), ac[idx], 0.0)

    enhanced = ac[lags] + 0.5 * at(2) + 0.25 * at(4)
    bpm_of = 60.0 / (lags * frame_s)
    prior = np.exp(-0.5 * (np.log2(bpm_of / _PRIOR_BPM) / _PRIOR_SIGMA_OCT) ** 2)
    score = enhanced * prior
    k = int(np.argmax(score))
    lag = float(lags[k])
    if 0 < k < len(lags) - 1:  # 抛物线插值，把 lag 精度提到亚帧
        a, b, c = score[k - 1], score[k], score[k + 1]
        denom = a - 2 * b + c
        if denom < 0:
            lag += 0.5 * (a - c) / denom
    conf = float(np.clip(ac[int(lags[k])], 0.0, 1.0))
    return float(np.clip(60.0 / (lag * frame_s), _MIN_BPM, _MAX_BPM)), conf


# ------------------------------------------------------------------ Ellis 2007 动态规划


def track_beats(env: np.ndarray, period: float, tightness: float = _TIGHTNESS) -> np.ndarray:
    """返回拍所在的帧索引。

    C(t) = O(t) + max(0, max_τ [C(τ) + F(t-τ)])，F(Δ) = -α·ln²(Δ/T)；
    τ 取 t-2T..t-T/2，每步对窗口向量化取最大。回溯起点：C 的局部极大里，高于其中位数一半的最后一个。
    """
    n = len(env)
    w_lo, w_hi = max(1, int(round(period / 2))), max(2, int(round(2 * period)))
    offsets = np.arange(w_lo, w_hi + 1)
    penalty = -tightness * np.log(offsets / period) ** 2
    cum = env.astype(np.float64).copy()
    back = np.full(n, -1, dtype=np.int64)
    for t in range(w_lo, n):
        prev = t - offsets  # 降序
        ok = prev >= 0
        if not ok.any():
            continue
        cand = cum[prev[ok]] + penalty[ok]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            cum[t] = env[t] + cand[j]
            back[t] = prev[ok][j]
    peaks = np.flatnonzero((cum[1:-1] >= cum[:-2]) & (cum[1:-1] >= cum[2:])) + 1
    if len(peaks) == 0:
        return np.zeros(0, dtype=np.int64)
    good = peaks[cum[peaks] >= 0.5 * np.median(cum[peaks])]
    t = int(good[-1])
    path = [t]
    while back[t] >= 0:
        t = int(back[t])
        path.append(t)
    return np.asarray(path[::-1], dtype=np.int64)


def _trim_weak_ends(beats: np.ndarray, env: np.ndarray) -> np.ndarray:
    """去掉首尾 onset 强度低于 "拍处平均强度的一半" 的拍（静音片头/片尾上的虚假拍）。"""
    if len(beats) < 3:
        return beats
    strength = env[beats]
    thr = 0.5 * float(np.sqrt(np.mean(strength**2)))
    lo, hi = 0, len(beats)
    while lo < hi - 1 and strength[lo] < thr:
        lo += 1
    while hi - 1 > lo and strength[hi - 1] < thr:
        hi -= 1
    return beats[lo:hi]


# ------------------------------------------------------------------ downbeat


def _band_envelope(x: np.ndarray, sr: int, band: tuple[float, float]) -> np.ndarray:
    """频域掩码带通后取平方、20ms 平滑 → 该频带的能量包络（逐样本）。"""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / sr)
    spec[(f < band[0]) | (f > band[1])] = 0
    y = np.fft.irfft(spec, len(x))
    w = max(1, int(0.02 * sr))
    return np.convolve(y * y, np.ones(w) / w, mode="same")


def _kick_strength(x: np.ndarray, sr: int, beat_t: np.ndarray) -> np.ndarray:
    """每拍的低频起音：拍后 80ms 的低频能量 - 拍前 80ms 的低频能量（≥0）。"""
    e = _band_envelope(x, sr, _LOW_BAND_HZ)
    n = len(e)
    a = int(0.08 * sr)
    out = np.zeros(len(beat_t))
    for i, t in enumerate(beat_t):
        c = int(round(t * sr))
        after = e[min(c, n - 1) : min(c + a, n)]
        before = e[max(c - a, 0) : max(c - int(0.01 * sr), 0)]
        out[i] = max(0.0, (after.mean() if after.size else 0.0) - (before.mean() if before.size else 0.0))
    return out


def _bass_change(x: np.ndarray, sr: int, beat_t: np.ndarray, interval: float) -> np.ndarray:
    """每拍与上一拍的低音频谱差异（1-cos）：只看每拍后 45%，避开底鼓/军鼓，留下持续的低音音高。"""
    feats = []
    for t in beat_t:
        a = int(round((t + (1 - _ACCENT_TAIL_FRAC) * interval) * sr))
        b = int(round((t + interval) * sr))
        seg = x[max(a, 0) : min(b, len(x))]
        if seg.size < int(0.05 * sr):
            feats.append(None)
            continue
        n_fft = 1 << int(np.ceil(np.log2(max(seg.size, sr))))  # 补零到 ≥1s：频率分辨率 ≤1Hz，分得清相邻的低音
        mag = np.abs(np.fft.rfft(seg * np.hanning(seg.size), n_fft))
        f = np.fft.rfftfreq(n_fft, 1.0 / sr)
        v = np.sqrt(mag[(f >= _BASS_BAND_HZ[0]) & (f <= _BASS_BAND_HZ[1])])
        feats.append(v / (np.linalg.norm(v) + 1e-12))
    out = np.zeros(len(beat_t))
    for i in range(1, len(feats)):
        if feats[i] is not None and feats[i - 1] is not None:
            out[i] = 1.0 - float(np.dot(feats[i], feats[i - 1]))
    return out


def pick_downbeat_phase(x: np.ndarray, sr: int, beat_t: np.ndarray, interval: float) -> int:
    """在 4 个相位里选小节首拍：底鼓起音最强 + 低音音高变化最大（两项各自按相位均值的相对偏离度相加）。

    只看底鼓会在 1、3 拍都有底鼓的曲子里无法分辨首拍和第 3 拍，所以加一项低音变化作为判据。
    """
    kick = _kick_strength(x, sr, beat_t)
    change = _bass_change(x, sr, beat_t, interval)
    score = np.zeros(4)
    for feat in (kick, change):
        phase_mean = np.array([feat[p::4].mean() if len(feat[p::4]) else 0.0 for p in range(4)])
        base = phase_mean.mean()
        if base > 1e-12:
            score += (phase_mean - base) / base
    log.debug("downbeat 相位得分 %s", np.round(score, 3))
    return int(np.argmax(score))


# ------------------------------------------------------------------ 入口


def detect_beats(x: np.ndarray, sr: int) -> BeatResult:
    """单声道信号 → BeatResult。tempo 限定 60–200 BPM，4/4 拍假设。

    输入过短（<2s）或静音时返回空结果（bpm=0, confidence=0）。
    """
    t0 = time.perf_counter()
    if len(x) < _MIN_DURATION_S * sr:
        log.warning("detect_beats: 输入只有 %.2fs，短于 %.1fs，无法估计节拍", len(x) / sr, _MIN_DURATION_S)
        return _EMPTY
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        log.warning("detect_beats: 输入是静音")
        return _EMPTY
    x = (x / peak).astype(np.float32)

    env, hop, _ = onset_strength(x, sr)
    frame_s = hop / sr
    bpm0, conf = estimate_tempo(env, frame_s)
    idx = track_beats(env, 60.0 / bpm0 / frame_s)
    idx = _trim_weak_ends(idx, env)
    if len(idx) < 2:
        log.warning("detect_beats: 只跟到 %d 拍（tempo≈%.1f）", len(idx), bpm0)
        return BeatResult(bpm0, tuple(idx * frame_s), (), conf)

    beat_t = idx * frame_s
    # 拍序列的最小二乘斜率比 DP 栅格上的单个间隔更准
    bpm = float(np.clip(60.0 / np.polyfit(np.arange(len(beat_t)), beat_t, 1)[0], _MIN_BPM, _MAX_BPM)) if len(beat_t) >= 4 else bpm0
    interval = 60.0 / bpm
    phase = pick_downbeat_phase(x, sr, beat_t, interval) if len(beat_t) >= 4 else 0
    downbeats = beat_t[phase::4]
    log.info(
        "detect_beats: %.1fs, tempo 自相关 %.2f → 跟拍后 %.2f BPM, %d 拍, %d 小节首拍(相位 %d), 置信度 %.2f, %.0fms",
        len(x) / sr, bpm0, bpm, len(beat_t), len(downbeats), phase, conf, (time.perf_counter() - t0) * 1e3,
    )
    return BeatResult(bpm, tuple(float(t) for t in beat_t), tuple(float(t) for t in downbeats), conf)
