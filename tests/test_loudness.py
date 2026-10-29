"""loudness：ebur128 积分响度 + 简化短时响度曲线（高光检测用）。"""

import logging
import wave
from pathlib import Path

import numpy as np
import pytest

from autocut.loudness import _k_weighting_biquads, integrated_lufs, k_weighting_gain, short_term_curve, top_peaks
from autocut.probe import MediaError

log = logging.getLogger(__name__)
SR = 16000


def _sine(freq: float, amp: float, sec: float, sr: int = SR) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * freq * np.arange(int(sec * sr)) / sr)).astype(np.float32)


def _write_wav(path: Path, x: np.ndarray, sr: int = SR) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


# ------------------------------------------------------------------ 单元：short_term_curve / top_peaks


@pytest.mark.unit
def test_k_weighting_matches_bs1770_coefficients_at_48k() -> None:
    """48 kHz 下的系数必须与 ITU-R BS.1770-4 表里的值一致，且两级滤波器合起来的幅度响应对得上。"""
    (b1, a1), (b2, a2) = _k_weighting_biquads(48000)
    assert b1 == pytest.approx([1.53512485958697, -2.69169618940638, 1.19839281085285], abs=1e-6)
    assert a1 == pytest.approx([1.0, -1.69065929318241, 0.73248077421585], abs=1e-6)
    assert b2 == pytest.approx([1.0, -2.0, 1.0])
    assert a2 == pytest.approx([1.0, -1.99004745483398, 0.99007225036621], abs=1e-6)
    f = np.array([20.0, 1000.0, 4000.0])
    assert 20 * np.log10(k_weighting_gain(f, 48000)) == pytest.approx([-13.28, 0.70, 3.97], abs=0.02)


@pytest.mark.unit
def test_short_term_curve_grid_and_constant_signal() -> None:
    x = _sine(1000, 0.1, 20.0)  # -20 dBFS 峰值的 1 kHz 正弦 ≈ -23 LUFS
    times, db = short_term_curve(x, SR, window=3.0, hop=0.5)
    assert times[0] == 0.0 and np.allclose(np.diff(times), 0.5)
    assert len(times) == len(db) == 41
    assert db[4:-4] == pytest.approx(-23.0, abs=0.7)


@pytest.mark.unit
def test_short_term_curve_peaks_at_center_of_burst() -> None:
    rng = np.random.default_rng(0)
    x = rng.standard_normal(SR * 30).astype(np.float32) * 0.003
    x[SR * 10 : SR * 13] += (rng.standard_normal(SR * 3) * 0.1).astype(np.float32)
    times, db = short_term_curve(x, SR)
    assert times[int(np.argmax(db))] == pytest.approx(11.5, abs=0.6)  # 时间轴是窗口中心
    assert db.max() - np.median(db) > 20


@pytest.mark.unit
def test_short_term_curve_k_weighting_penalizes_low_frequencies() -> None:
    """K 加权：同样幅度的 20 Hz 低频比 1 kHz 响度低很多，而 4 kHz 略高于 1 kHz。"""
    level = {f: short_term_curve(_sine(f, 0.1, 10.0), SR)[1][4:-4].mean() for f in (20, 1000, 4000)}
    assert level[20] < level[1000] - 8
    assert 0.5 < level[4000] - level[1000] < 6


@pytest.mark.unit
def test_short_term_curve_silence_floor_and_short_input() -> None:
    times, db = short_term_curve(np.zeros(SR * 5, np.float32), SR)
    assert db.max() <= -70
    t2, d2 = short_term_curve(np.zeros(100, np.float32), SR)
    assert len(t2) == len(d2) >= 1  # 比窗口还短也给一个点，不崩


@pytest.mark.unit
def test_top_peaks_nms_min_distance() -> None:
    times = np.arange(0, 100, 0.5)
    db = -40 + np.zeros_like(times)
    for c, h in [(10, 12), (18, 10), (50, 8), (90, 15)]:
        db += h * np.exp(-0.5 * ((times - c) / 1.5) ** 2)
    peaks = top_peaks(times, db, n=3, min_distance=20.0)
    assert [round(t) for t, _ in peaks] == [90, 10, 50]  # 按高度降序；18s 的峰被 10s 的压制
    assert all(a[1] >= b[1] for a, b in zip(peaks, peaks[1:]))
    assert len(top_peaks(times, db, n=10, min_distance=20.0)) <= 5


@pytest.mark.unit
def test_top_peaks_on_flat_or_empty() -> None:
    assert top_peaks(np.zeros(0), np.zeros(0), n=3, min_distance=5.0) == []
    t = np.arange(10.0)
    assert len(top_peaks(t, np.full(10, -30.0), n=3, min_distance=2.0)) <= 1


# ------------------------------------------------------------------ 集成：ebur128 / 素材


@pytest.mark.integration
def test_integrated_lufs_of_reference_sine(tmp_path) -> None:
    """1 kHz、-20 dBFS 峰值的单声道正弦 ≈ -23 LUFS（BS.1770 的参考电平）。"""
    wav = _write_wav(tmp_path / "ref.wav", _sine(1000, 0.1, 10.0))
    assert integrated_lufs(wav) == pytest.approx(-23.0, abs=0.5)


@pytest.mark.integration
def test_numpy_short_term_agrees_with_ffmpeg_ebur128(tmp_path) -> None:
    """自己实现的 K 加权短时响度与 ffmpeg 的积分响度在稳态信号上应当吻合（±0.7 LU）。"""
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(SR * 12) * 0.05).astype(np.float32)
    wav = _write_wav(tmp_path / "noise.wav", x)
    _, db = short_term_curve(x, SR)
    assert db[4:-4].mean() == pytest.approx(integrated_lufs(wav), abs=0.7)


@pytest.mark.integration
def test_integrated_lufs_talk_zh(media) -> None:
    lufs = integrated_lufs(media / "talk_zh.mp4")
    log.info("talk_zh integrated = %.2f LUFS", lufs)
    assert np.isfinite(lufs) and -40 < lufs < -5


@pytest.mark.integration
def test_integrated_lufs_selects_track(media) -> None:
    p = media / "game_commentary.mp4"
    mic, game = integrated_lufs(p, track="mic"), integrated_lufs(p, track="game")
    log.info("game_commentary: mic=%.2f game=%.2f mix=%.2f", mic, game, integrated_lufs(p, track=0))
    assert mic != pytest.approx(game, abs=0.3)
    assert integrated_lufs(p, track=1) == pytest.approx(mic, abs=1e-6)


@pytest.mark.integration
def test_integrated_lufs_errors(media, tmp_path) -> None:
    with pytest.raises(MediaError, match="ghost.mp4"):
        integrated_lufs(tmp_path / "ghost.mp4")
    with pytest.raises(MediaError):
        integrated_lufs(media / "game_commentary.mp4", track="nope")


@pytest.mark.integration
def test_stream_vod_loudness_peaks_land_on_highlights(audio_of, truth) -> None:
    """3 个真高光（大喊+笑）的响度明显高于闲聊：前 3 个局部峰（间隔 ≥20s）各落在一个高光 ±5s 内。"""
    hl = truth("stream_vod")["highlights"]
    x = audio_of("stream_vod.mp4")
    times, db = short_term_curve(x, 16000, window=3.0, hop=0.5)
    peaks = top_peaks(times, db, n=3, min_distance=20.0)
    log.info("stream_vod 峰值：%s | 高光：%s", [(round(t, 1), round(v, 1)) for t, v in peaks],
             [(h["start"], h["end"]) for h in hl])
    assert len(peaks) == 3
    hit = set()
    for t, _ in peaks:
        idx = [i for i, h in enumerate(hl) if h["start"] - 5 <= t <= h["end"] + 5]
        assert idx, f"峰值 {t:.1f}s 不在任何高光 ±5s 内"
        hit.add(idx[0])
    assert hit == {0, 1, 2}, "三个峰应当一一对应三个高光"
