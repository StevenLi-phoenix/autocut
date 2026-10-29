"""beats：onset 强度 → tempo → DP 节拍跟踪 → downbeat。"""

import logging

import numpy as np
import pytest

from autocut.beats import BeatResult, detect_beats

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ 合成鼓点


def _drum_track(bpm: float, sec: float, sr: int = 22050, offset: float = 0.0, seed: int = 0, pulse_gain: float = 0.25):
    """每拍一个军鼓/踩镲式的宽带噪声脉冲，1、3 拍加低频底鼓，第 1 拍的底鼓更重。

    返回 (信号, 真值拍时刻, 真值小节首拍时刻)。
    """
    rng = np.random.default_rng(seed)
    spb = 60.0 / bpm
    y = np.zeros(int(sec * sr), dtype=np.float64)
    beats = np.arange(offset, sec - 0.3, spb)
    t_k = np.arange(int(0.25 * sr)) / sr
    kick = np.sin(2 * np.pi * np.cumsum(50 + 110 * np.exp(-t_k * 35)) / sr) * np.exp(-t_k * 10)
    t_n = np.arange(int(0.08 * sr)) / sr
    for i, b in enumerate(beats):
        j = int(round(b * sr))
        noise = rng.standard_normal(t_n.size) * np.exp(-t_n * 60) * pulse_gain
        y[j : j + noise.size] += noise[: y.size - j]
        if i % 2 == 0:
            gain = 1.0 if i % 4 == 0 else 0.7
            y[j : j + kick.size] += (kick * gain)[: y.size - j]
    y /= np.abs(y).max()
    return y.astype(np.float32), beats, beats[::4]


def _match_fraction(truth_t: np.ndarray, det_t: np.ndarray, tol: float) -> float:
    """真值里有多少比例在 ±tol 内有检测值。"""
    if len(det_t) == 0:
        return 0.0
    d = np.abs(truth_t[:, None] - np.asarray(det_t)[None, :]).min(axis=1)
    return float(np.mean(d <= tol))


@pytest.mark.unit
@pytest.mark.parametrize("bpm", [75.0, 90.0, 120.0, 140.0, 170.0])
def test_tempo_and_beats_on_synthetic_drums(bpm: float) -> None:
    sr = 22050
    x, beats, bars = _drum_track(bpm, 24.0, sr=sr, offset=0.37)
    res = detect_beats(x, sr)
    assert isinstance(res, BeatResult)
    assert res.bpm == pytest.approx(bpm, abs=1.0)
    assert _match_fraction(beats, np.asarray(res.beats), 0.025) >= 0.9
    assert 0.0 <= res.confidence <= 1.0


@pytest.mark.unit
def test_downbeats_are_every_fourth_beat_on_kick_accents() -> None:
    sr = 22050
    x, beats, bars = _drum_track(120.0, 24.0, sr=sr, offset=0.25)
    res = detect_beats(x, sr)
    db = np.asarray(res.downbeats)
    assert len(db) >= len(bars) - 1
    assert _match_fraction(bars, db, 0.025) >= 0.75
    # downbeats 一定是 beats 的子集，且间隔恰好 4 拍
    assert all(np.min(np.abs(np.asarray(res.beats) - d)) < 1e-9 for d in db)
    idx = [int(np.argmin(np.abs(np.asarray(res.beats) - d))) for d in db]
    assert set(np.diff(idx)) == {4}


@pytest.mark.unit
def test_octave_error_is_avoided_with_weak_prior() -> None:
    """底鼓（隔拍）远强于每拍的噪声脉冲时，自相关在 2 倍拍长处更高；不能因此报成 60 BPM。"""
    sr = 22050
    x, beats, _ = _drum_track(120.0, 24.0, sr=sr, pulse_gain=0.06)
    res = detect_beats(x, sr)
    assert res.bpm == pytest.approx(120.0, abs=1.5)
    assert _match_fraction(beats, np.asarray(res.beats), 0.025) >= 0.9


@pytest.mark.unit
def test_tempo_stays_in_60_200_range() -> None:
    sr = 22050
    for bpm in (62.0, 198.0):
        x, _, _ = _drum_track(bpm, 24.0, sr=sr)
        assert 60.0 <= detect_beats(x, sr).bpm <= 200.0


@pytest.mark.unit
def test_silence_and_noise_do_not_crash_and_have_low_confidence() -> None:
    sr = 22050
    silent = detect_beats(np.zeros(sr * 5, np.float32), sr)
    assert silent.beats == () and silent.confidence == 0.0
    rng = np.random.default_rng(3)
    noise = detect_beats((rng.standard_normal(sr * 10) * 0.1).astype(np.float32), sr)
    x, _, _ = _drum_track(120.0, 10.0, sr=sr)
    assert noise.confidence < detect_beats(x, sr).confidence


@pytest.mark.unit
def test_too_short_input_returns_empty_result() -> None:
    res = detect_beats(np.zeros(1000, np.float32), 22050)
    assert res.beats == () and res.downbeats == ()


@pytest.mark.unit
def test_works_at_other_sample_rates() -> None:
    x, beats, _ = _drum_track(100.0, 20.0, sr=16000)
    res = detect_beats(x, 16000)
    assert res.bpm == pytest.approx(100.0, abs=1.0)
    assert _match_fraction(beats, np.asarray(res.beats), 0.025) >= 0.9


# ------------------------------------------------------------------ 集成：montage / ytpmv 音乐


@pytest.mark.integration
@pytest.mark.parametrize("rel, truth_name", [("montage/music_120bpm.wav", "montage"), ("ytpmv/music_120bpm.wav", "ytpmv")])
def test_beats_on_fixture_music(rel, truth_name, audio_of, truth) -> None:
    t = truth(truth_name)
    x = audio_of(rel, sr=22050)
    res = detect_beats(x, 22050)
    tb = np.asarray(t["beats"])
    err = np.abs(tb[:, None] - np.asarray(res.beats)[None, :]).min(axis=1)
    log.info("%s: bpm=%.2f conf=%.2f 拍=%d/%d 误差中位=%.1fms p90=%.1fms max=%.1fms", truth_name, res.bpm,
             res.confidence, len(res.beats), len(tb), np.median(err) * 1e3, np.percentile(err, 90) * 1e3, err.max() * 1e3)
    assert res.bpm == pytest.approx(t["bpm"], abs=1.0)
    assert _match_fraction(tb, np.asarray(res.beats), 0.025) >= 0.9
    # 不多报：检测拍数不应明显多于真值（防止把八分音符也当成拍）
    assert len(res.beats) <= len(tb) * 1.1
    assert res.confidence > 0.3


@pytest.mark.integration
def test_downbeats_on_montage_music(audio_of, truth) -> None:
    t = truth("montage")
    res = detect_beats(audio_of("montage/music_120bpm.wav", sr=22050), 22050)
    bars = np.asarray(t["bar_starts"])
    recall = _match_fraction(bars, np.asarray(res.downbeats), 0.025)
    precision = _match_fraction(np.asarray(res.downbeats), bars, 0.025)
    log.info("montage downbeats: 检测=%d 真值=%d recall=%.2f precision=%.2f", len(res.downbeats), len(bars), recall, precision)
    assert recall >= 0.75
    assert precision >= 0.75


@pytest.mark.integration
def test_beats_robust_to_input_sample_rate(audio_of, truth) -> None:
    """16k 输入（load_audio 默认值）也要能跟上拍。"""
    t = truth("montage")
    res = detect_beats(audio_of("montage/music_120bpm.wav", sr=16000), 16000)
    assert res.bpm == pytest.approx(120.0, abs=1.0)
    assert _match_fraction(np.asarray(t["beats"]), np.asarray(res.beats), 0.025) >= 0.9
