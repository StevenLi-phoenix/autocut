"""audio：ffmpeg 管道解码 + 帧级 dB。"""

import numpy as np
import pytest

from autocut.audio import frame_db, load_audio, resolve_track, windowed_ms
from autocut.probe import MediaError

# ------------------------------------------------------------------ 单元：frame_db / windowed_ms


def _sine(freq: float, amp: float, sec: float, sr: int = 16000) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * freq * np.arange(int(sec * sr)) / sr)).astype(np.float32)


@pytest.mark.unit
def test_frame_db_of_sine_is_rms_dbfs() -> None:
    x = _sine(440, 0.5, 2.0)
    db = frame_db(x, 16000)
    expected = 20 * np.log10(0.5 / np.sqrt(2))  # -9.03 dBFS
    assert db[10:-10] == pytest.approx(expected, abs=0.1)


@pytest.mark.unit
def test_frame_db_shape_and_hop() -> None:
    x = np.zeros(16000 * 3, dtype=np.float32)
    db = frame_db(x, 16000, hop=0.01)
    assert db.dtype == np.float32
    assert len(db) == 301  # 每 10ms 一帧，含首尾，帧 i 以 i*hop 为中心
    assert db[0] == -100.0


@pytest.mark.unit
def test_frame_db_silence_floor_and_tracks_step() -> None:
    x = np.concatenate([np.zeros(16000, np.float32), _sine(300, 0.2, 1.0)])
    db = frame_db(x, 16000)
    assert db[:90].max() == -100.0
    assert db[110:190].min() > -20
    # 阶跃出现在 1.0s 附近，帧中心对齐 i*hop
    assert 95 <= int(np.argmax(db > -50)) <= 105


@pytest.mark.unit
def test_frame_db_edges_not_biased_by_padding() -> None:
    """首尾帧的窗口超出信号，不能因补零把电平拉低。"""
    db = frame_db(_sine(440, 0.5, 1.0), 16000)
    assert abs(db[0] - db[50]) < 3.0 and abs(db[-1] - db[50]) < 3.0


@pytest.mark.unit
def test_frame_db_empty_input() -> None:
    assert frame_db(np.zeros(0, np.float32), 16000).size == 0


@pytest.mark.unit
def test_windowed_ms_matches_naive() -> None:
    rng = np.random.default_rng(0)
    x = rng.standard_normal(8000).astype(np.float32) * 0.1
    ms = windowed_ms(x, 8000, hop=0.05, win=0.1)
    t = 20  # 中心 1.0s，窗口 [0.95, 1.05)
    c, h = int(1.0 * 8000), int(0.05 * 8000)
    naive = np.mean(x[c - h : c + h].astype(np.float64) ** 2)
    assert ms[t] == pytest.approx(naive, rel=0.02)


# ------------------------------------------------------------------ 集成：load_audio


@pytest.mark.integration
def test_load_audio_basic(media, truth) -> None:
    x = load_audio(media / "talk_zh.mp4")
    assert x.dtype == np.float32 and x.ndim == 1
    assert len(x) / 16000 == pytest.approx(truth("talk_zh")["duration"], abs=0.05)
    assert np.abs(x).max() <= 1.0 and np.abs(x).max() > 0.05


@pytest.mark.integration
def test_load_audio_sample_rate(media) -> None:
    x = load_audio(media / "talk_zh.mp4", sr=8000)
    assert len(x) / 8000 == pytest.approx(40.74, abs=0.05)


@pytest.mark.integration
def test_load_audio_start_end(media) -> None:
    x = load_audio(media / "talk_zh.mp4", start=3.0, end=5.5)
    assert len(x) / 16000 == pytest.approx(2.5, abs=0.02)
    full = load_audio(media / "talk_zh.mp4")
    seg = full[int(3.0 * 16000) : int(5.5 * 16000)]
    n = min(len(x), len(seg))
    # 切片与整段解码的同一区间应当高度一致（容许 seek 带来的少量边缘差异）
    assert np.corrcoef(x[:n], seg[:n])[0, 1] > 0.98


@pytest.mark.integration
def test_load_audio_start_only_and_end_only(media) -> None:
    a = load_audio(media / "talk_zh.mp4", start=38.0)
    assert len(a) / 16000 == pytest.approx(40.74 - 38.0, abs=0.05)
    b = load_audio(media / "talk_zh.mp4", end=2.0)
    assert len(b) / 16000 == pytest.approx(2.0, abs=0.02)


@pytest.mark.integration
def test_load_audio_selects_track_by_index_and_title(media) -> None:
    p = media / "game_commentary.mp4"
    mix = load_audio(p, track=0)
    mic_i = load_audio(p, track=1)
    mic_t = load_audio(p, track="mic")
    game = load_audio(p, track="game")
    assert np.array_equal(mic_i, mic_t)
    assert not np.allclose(mix[: len(mic_i)], mic_i[: len(mix)], atol=1e-3)
    # mic 轨只有人声+底噪：爆炸段（19.7–22.9s）很安静；game 轨则相反
    seg = slice(int(20.0 * 16000), int(22.5 * 16000))
    assert np.sqrt(np.mean(mic_i[seg] ** 2)) < 0.3 * np.sqrt(np.mean(game[seg] ** 2))
    assert load_audio(p).shape == mix.shape and np.array_equal(load_audio(p), mix)  # 默认第 0 条


@pytest.mark.integration
def test_load_audio_bad_track_raises_with_available_titles(media) -> None:
    p = media / "game_commentary.mp4"
    with pytest.raises(MediaError) as ei:
        load_audio(p, track="nope")
    assert "nope" in str(ei.value) and "mic" in str(ei.value)
    with pytest.raises(MediaError, match="track"):
        load_audio(p, track=7)


@pytest.mark.integration
def test_load_audio_missing_file_and_no_audio(media, tmp_path) -> None:
    with pytest.raises(MediaError, match="ghost.mp4"):
        load_audio(tmp_path / "ghost.mp4")
    from conftest import run_ffmpeg

    silent_video = tmp_path / "video_only.mp4"
    run_ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x36:r=10:d=1", str(silent_video))
    with pytest.raises(MediaError, match="video_only.mp4"):
        load_audio(silent_video)


@pytest.mark.integration
def test_resolve_track(media) -> None:
    p = media / "game_commentary.mp4"
    assert resolve_track(p, None) == 0
    assert resolve_track(p, 2) == 2
    assert resolve_track(p, "game") == 2
    assert resolve_track(p, "MIC") == 1  # 标题大小写不敏感
