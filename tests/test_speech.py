"""speech：自适应阈值的语音/静音检测。"""

import logging
import time

import numpy as np
import pytest

from autocut.audio import frame_db, load_audio
from autocut.speech import SpeechResult, detect_speech, invert_regions
from conftest import coverage

log = logging.getLogger(__name__)
SR = 16000

# ------------------------------------------------------------------ 合成信号


def _noise(sec: float, dbfs: float, rng: np.random.Generator) -> np.ndarray:
    return (rng.standard_normal(int(round(sec * SR))) * 10 ** (dbfs / 20)).astype(np.float32)


def _burst(sec: float, dbfs: float, rng: np.random.Generator) -> np.ndarray:
    """带 15ms 渐入渐出的噪声段，充当"语音"。"""
    x = rng.standard_normal(int(sec * SR))
    ramp = int(0.015 * SR)
    env = np.ones_like(x)
    env[:ramp] = np.linspace(0, 1, ramp)
    env[-ramp:] = np.linspace(1, 0, ramp)
    return (x * env * 10 ** (dbfs / 20)).astype(np.float32)


def _scene(floor_db: float, speech_db: float, layout: list[tuple[str, float]], seed: int = 0):
    """layout：[("gap", 秒) | ("speech", 秒), ...]；底噪贯穿全程。返回 (信号, 真值语音区间)。"""
    rng = np.random.default_rng(seed)
    parts, truth, t = [], [], 0.0
    for kind, sec in layout:
        parts.append(_burst(sec, speech_db, rng) if kind == "speech" else np.zeros(int(sec * SR), np.float32))
        if kind == "speech":
            truth.append((t, t + sec))
        t += sec
    x = np.concatenate(parts)
    return x + _noise(len(x) / SR, floor_db, rng)[: len(x)], truth


LAYOUT = [("gap", 1.0), ("speech", 1.5), ("gap", 0.8), ("speech", 2.0), ("gap", 1.2), ("speech", 0.6), ("gap", 1.0)]


@pytest.mark.unit
@pytest.mark.parametrize("floor_db", [-70.0, -52.0, -38.0, -30.0])
def test_adapts_to_noise_floor(floor_db: float) -> None:
    """语音比底噪高 25 dB；无论底噪是 -70 还是 -30 dBFS，边界都应落在真值 ±60ms 内。"""
    x, truth = _scene(floor_db, floor_db + 25, LAYOUT)
    res = detect_speech(x, SR)
    assert len(res.regions) == len(truth)
    for (s, e), (ts, te) in zip(res.regions, truth):
        assert abs(s - ts) < 0.06 and abs(e - te) < 0.06
    assert res.noise_floor_db == pytest.approx(floor_db, abs=3)
    assert floor_db + 6 <= res.threshold_db < res.speech_level_db
    assert res.frame_hop == pytest.approx(0.01)


@pytest.mark.unit
def test_threshold_is_at_least_6db_above_noise_floor() -> None:
    """语音只比底噪高 12 dB 时，阈值也不能贴着底噪。"""
    x, _ = _scene(-50.0, -38.0, LAYOUT)
    res = detect_speech(x, SR)
    assert res.threshold_db >= res.noise_floor_db + 6 - 1e-6


@pytest.mark.unit
def test_short_pause_merged_long_pause_split() -> None:
    x, _ = _scene(-60.0, -30.0, [("gap", 0.5), ("speech", 1.0), ("gap", 0.25), ("speech", 1.0), ("gap", 0.5)])
    assert len(detect_speech(x, SR).regions) == 1  # 默认 min_silence=0.4：0.25s 的逗号停顿被并掉
    y, _ = _scene(-60.0, -30.0, [("gap", 0.5), ("speech", 1.0), ("gap", 0.8), ("speech", 1.0), ("gap", 0.5)])
    assert len(detect_speech(y, SR).regions) == 2  # 0.8s 的停顿保留
    assert len(detect_speech(y, SR, min_silence=1.0).regions) == 1  # min_silence 可调
    assert len(detect_speech(x, SR, min_silence=0.1).regions) == 2


@pytest.mark.unit
def test_short_blip_removed() -> None:
    x, _ = _scene(-60.0, -30.0, [("gap", 1.0), ("speech", 0.02), ("gap", 1.0), ("speech", 1.0), ("gap", 1.0)])
    res = detect_speech(x, SR, min_speech=0.08)
    assert len(res.regions) == 1 and res.regions[0][0] > 1.9
    assert len(detect_speech(x, SR, min_speech=0.0).regions) == 2


@pytest.mark.unit
def test_noise_only_yields_no_speech() -> None:
    rng = np.random.default_rng(1)
    res = detect_speech(_noise(5.0, -50.0, rng), SR)
    assert res.regions == ()


@pytest.mark.unit
def test_digital_silence_and_empty_input() -> None:
    assert detect_speech(np.zeros(SR * 2, np.float32), SR).regions == ()
    assert detect_speech(np.zeros(0, np.float32), SR).regions == ()


@pytest.mark.unit
def test_explicit_threshold_overrides_estimate() -> None:
    x, _ = _scene(-60.0, -30.0, LAYOUT)
    res = detect_speech(x, SR, threshold_db=-20.0)
    assert res.threshold_db == -20.0
    assert res.regions == ()  # 语音电平 -30 dBFS 低于手动阈值


@pytest.mark.unit
def test_pad_extends_and_clips_to_signal() -> None:
    x, truth = _scene(-60.0, -30.0, LAYOUT)
    base = detect_speech(x, SR, pad=0.0)
    padded = detect_speech(x, SR, pad=0.1)
    assert len(padded.regions) == len(base.regions)
    for (bs, be), (ps, pe) in zip(base.regions, padded.regions):
        assert ps == pytest.approx(max(0.0, bs - 0.1), abs=0.011) and pe == pytest.approx(be + 0.1, abs=0.011)
    assert padded.regions[-1][1] <= len(x) / SR + 1e-9
    # 两段间隔 0.8s，各扩 0.5 会重叠 → 必须合并而不是输出相交区间
    merged = detect_speech(x, SR, pad=0.5)
    assert all(a[1] < b[0] for a, b in zip(merged.regions, merged.regions[1:]))


@pytest.mark.unit
def test_regions_sorted_disjoint_within_signal() -> None:
    x, _ = _scene(-45.0, -20.0, LAYOUT)
    res = detect_speech(x, SR)
    flat = [t for r in res.regions for t in r]
    assert flat == sorted(flat)
    assert all(s < e for s, e in res.regions)
    assert res.regions[0][0] >= 0 and res.regions[-1][1] <= len(x) / SR + 1e-9


@pytest.mark.unit
def test_invert_regions() -> None:
    assert invert_regions([(1.0, 2.0), (3.0, 4.0)], 5.0) == [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]
    assert invert_regions([], 3.0) == [(0.0, 3.0)]
    assert invert_regions([(0.0, 3.0)], 3.0) == []


# ------------------------------------------------------------------ 集成：真值素材


def _evaluate(res: SpeechResult, segments: list[dict], duration: float, label: str) -> tuple[float, float]:
    """返回 (最差的语音召回, 所有 ≥1.0s 停顿里最差的"多出静音余量")。

    语音召回 = 真值语音段中被检测为语音的比例；
    静音余量 = 检测为静音的时长 - (停顿长度 - 0.4s)，≥0 即达标。
    """
    worst_recall, worst_slack = 1.0, float("inf")
    for seg in segments:
        a, b = seg["start"], min(seg["end"], duration)
        if seg["kind"] == "speech":
            worst_recall = min(worst_recall, coverage(res.regions, a, b) / (b - a))
        elif b - a >= 1.0:
            silent = (b - a) - coverage(res.regions, a, b)
            worst_slack = min(worst_slack, silent - (b - a - 0.4))
    log.info("%s: threshold=%.1f floor=%.1f speech=%.1f dB | 最差语音召回=%.3f 最差静音余量=%.3fs",
             label, res.threshold_db, res.noise_floor_db, res.speech_level_db, worst_recall, worst_slack)
    return worst_recall, worst_slack


@pytest.mark.integration
@pytest.mark.parametrize("name", ["talk_zh", "lecture_en"])
def test_detect_speech_on_truth_videos(name, audio_of, truth) -> None:
    t = truth(name)
    x = audio_of(f"{name}.mp4")
    res = detect_speech(x, 16000)
    recall, slack = _evaluate(res, t["segments"], t["duration"], name)
    assert recall >= 0.95
    assert slack >= 0.0
    if name == "lecture_en":
        # 底噪 -38 dBFS：固定 -40 dB 阈值会把整段噪声当语音，自适应阈值必须高于它
        assert res.threshold_db > -38
        assert res.noise_floor_db == pytest.approx(t["noise_dbfs"], abs=4)


@pytest.mark.integration
def test_detect_speech_on_game_mic_track(audio_of, truth) -> None:
    t = truth("game_commentary")
    res = detect_speech(audio_of("game_commentary.mp4", track="mic"), 16000)
    recall, slack = _evaluate(res, t["segments"], t["duration"], "game_commentary/mic")
    assert recall >= 0.95
    assert slack >= 0.0


@pytest.mark.integration
def test_detect_speech_on_game_mix_track_is_only_recorded(audio_of, truth) -> None:
    """mix 轨混了持续的游戏 BGM 和爆炸：只记录表现，不要求通过（语音/静音分类在混音上本来就不可靠，
    这正是 agent 需要先选 mic 轨的原因）。

    实测（2026-10-28）：底噪 -32.8 / 语音电平 -18.0 dBFS，动态范围只有 15 dB，阈值 -25.4 dBFS；
    语音召回仍是 1.00，但 BGM 被当成语音，≥1.0s 的停顿里最差的一个比要求少检出 6.6s 静音。
    """
    t = truth("game_commentary")
    res = detect_speech(audio_of("game_commentary.mp4", track="mix"), 16000)
    recall, slack = _evaluate(res, t["segments"], t["duration"], "game_commentary/mix（仅记录）")
    # 只断言不崩溃且给出了合法结果
    assert 0.0 <= recall <= 1.0 and np.isfinite(res.threshold_db)


@pytest.mark.integration
def test_speech_mask_agrees_with_truth_frame_by_frame(audio_of, truth) -> None:
    """帧级一致率（talk_zh）：不只看召回，也看误报——静音处不能大面积判成语音。"""
    t = truth("talk_zh")
    x = audio_of("talk_zh.mp4")
    res = detect_speech(x, 16000)
    n = len(frame_db(x, 16000))
    ts = np.arange(n) * res.frame_hop
    pred = np.zeros(n, bool)
    for s, e in res.regions:
        pred[(ts >= s) & (ts < e)] = True
    gt = np.zeros(n, bool)
    for seg in t["segments"]:
        if seg["kind"] == "speech":
            gt[(ts >= seg["start"]) & (ts < seg["end"])] = True
    agree = float(np.mean(pred == gt))
    log.info("talk_zh 帧级一致率=%.4f", agree)
    assert agree > 0.95


@pytest.mark.integration
def test_load_and_detect_speed_on_150s_stream(media) -> None:
    """stream_vod（150s）上 load_audio + detect_speech 总耗时 < 3s。"""
    t0 = time.perf_counter()
    x = load_audio(media / "stream_vod.mp4")
    t1 = time.perf_counter()
    res = detect_speech(x, 16000)
    t2 = time.perf_counter()
    log.info("stream_vod 150s: load_audio=%.3fs detect_speech=%.3fs 总计=%.3fs regions=%d",
             t1 - t0, t2 - t1, t2 - t0, len(res.regions))
    assert t2 - t0 < 3.0
    