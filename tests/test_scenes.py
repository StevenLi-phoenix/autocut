"""scenes：缩略灰度帧差 + 直方图差的自适应切点检测。"""

import logging
from pathlib import Path

import numpy as np
import pytest

from autocut.probe import MediaError
from autocut.scenes import detect_cuts, frame_scores, pick_cut_frames
from conftest import run_ffmpeg

log = logging.getLogger(__name__)
FRAME = 1 / 30

# ------------------------------------------------------------------ 单元：纯 numpy 的打分与选点


def _pattern(seed: int, h: int = 36, w: int = 64) -> np.ndarray:
    """一幅平滑的随机"画面"（低分辨率噪声放大），不同 seed 之间差异很大。"""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, size=(6, 8)).astype(np.float64)
    return np.kron(small, np.ones((h // 6, w // 8)))


def _shot(seed: int, n: int, jitter: float = 2.0, drift: float = 0.0, rng_seed: int = 0) -> np.ndarray:
    """同一镜头的 n 帧：基础画面 + 传感器噪声 + 缓慢整体亮度漂移。"""
    rng = np.random.default_rng(rng_seed + seed)
    base = _pattern(seed)
    frames = [np.clip(base + drift * i + rng.normal(0, jitter, base.shape), 0, 255) for i in range(n)]
    return np.stack(frames).astype(np.uint8)


@pytest.mark.unit
def test_frame_scores_zero_for_identical_and_large_at_cut() -> None:
    frames = np.concatenate([_shot(1, 30, jitter=0.0), _shot(2, 30, jitter=0.0)])
    s = frame_scores(frames)
    assert s.shape == (60,)
    assert s[0] == 0.0  # 第 0 帧没有前驱
    assert np.all(s[1:30] == 0.0) and np.all(s[31:] == 0.0)
    assert s[30] > 0.3


@pytest.mark.unit
def test_frame_scores_carries_prev_frame_across_chunks() -> None:
    frames = np.concatenate([_shot(1, 20), _shot(2, 20)])
    whole = frame_scores(frames)
    a = frame_scores(frames[:25])
    b = frame_scores(frames[25:], prev=frames[24])
    assert np.allclose(np.concatenate([a, b]), whole)


@pytest.mark.unit
def test_pick_cuts_finds_hard_cuts_not_noise() -> None:
    frames = np.concatenate([_shot(1, 40), _shot(2, 40), _shot(3, 40)])
    cuts, thr = pick_cut_frames(frame_scores(frames))
    assert cuts == [40, 80]
    assert thr > 0


@pytest.mark.unit
def test_pick_cuts_ignores_slow_brightness_drift_and_static_noise() -> None:
    drift = _shot(1, 120, jitter=1.0, drift=0.8)
    assert pick_cut_frames(frame_scores(drift))[0] == []
    static = _shot(2, 120, jitter=0.0)
    assert pick_cut_frames(frame_scores(static))[0] == []  # MAD=0 时靠绝对下限，不能每帧都报


@pytest.mark.unit
def test_pick_cuts_min_gap_merges_flash_pairs() -> None:
    """单帧闪白会产生"进+出"两个相邻大差值，只保留一个，避免一次事件报两个切点。"""
    a, b = _shot(1, 40), _shot(2, 40)
    flash = np.full((1, 36, 64), 255, np.uint8)
    frames = np.concatenate([a, flash, b])
    cuts, _ = pick_cut_frames(frame_scores(frames))
    assert len(cuts) == 1 and cuts[0] in (40, 41)


@pytest.mark.unit
def test_pick_cuts_ignores_sub_minimum_shots_at_edges() -> None:
    """镜头短于 3 帧不成立：片头第 1 帧的初始化跳变、片尾最后一帧的突变都不算切点。"""
    frames = np.concatenate([_shot(1, 1), _shot(2, 60), _shot(3, 1)])
    assert pick_cut_frames(frame_scores(frames))[0] == []
    frames = np.concatenate([_shot(1, 3), _shot(2, 30), _shot(3, 3)])
    assert pick_cut_frames(frame_scores(frames))[0] == [3, 33]  # 恰好 3 帧的镜头保留


@pytest.mark.unit
def test_pick_cuts_explicit_threshold() -> None:
    frames = np.concatenate([_shot(1, 30), _shot(2, 30)])
    s = frame_scores(frames)
    assert pick_cut_frames(s, threshold=0.99)[0] == []
    assert pick_cut_frames(s, threshold=0.2)[0] == [30]


@pytest.mark.unit
def test_pick_cuts_empty_or_single_frame() -> None:
    assert pick_cut_frames(np.zeros(0))[0] == []
    assert pick_cut_frames(np.zeros(1))[0] == []


# ------------------------------------------------------------------ 集成：真实视频


def _concat(media: Path, names: list[str], out: Path) -> Path:
    inputs = [a for n in names for a in ("-i", str(media / "montage" / f"{n}.mp4"))]
    chain = "".join(f"[{i}:v]" for i in range(len(names))) + f"concat=n={len(names)}:v=1:a=0"
    run_ffmpeg(*inputs, "-filter_complex", chain, "-r", "30", str(out))
    return out


def _split_detected(cuts: list[float], expected: list[float], tol: float) -> tuple[list[float], list[float]]:
    """返回 (每个期望切点的最近检测误差, 不在任何期望切点附近的误报)。"""
    errs = [min((abs(c - e) for c in cuts), default=float("inf")) for e in expected]
    false_pos = [c for c in cuts if all(abs(c - e) > tol for e in expected)]
    return errs, false_pos


@pytest.mark.integration
def test_cuts_on_three_concatenated_clips(media, tmp_path) -> None:
    video = _concat(media, ["clip1", "clip2", "clip3"], tmp_path / "three.mp4")
    cuts = detect_cuts(video)
    errs, false_pos = _split_detected(cuts, [4.0, 8.0], tol=1.5 * FRAME)
    log.info("三段拼接：cuts=%s 误差=%s 误报=%s", cuts, errs, false_pos)
    assert all(e <= FRAME + 1e-6 for e in errs)  # 4.0s / 8.0s 的 ±1 帧
    assert len(false_pos) <= 1


@pytest.mark.integration
def test_cuts_on_all_six_clips_and_cut_is_first_frame_of_new_shot(media, tmp_path) -> None:
    video = _concat(media, [f"clip{i}" for i in range(1, 7)], tmp_path / "six.mp4")
    cuts = detect_cuts(video)
    expected = [4.0, 8.0, 12.0, 16.0, 20.0]
    errs, false_pos = _split_detected(cuts, expected, tol=1.5 * FRAME)
    log.info("六段拼接：cuts=%s 误差=%s 误报=%s", cuts, errs, false_pos)
    assert all(e <= FRAME + 1e-6 for e in errs)
    assert len(false_pos) <= 1


@pytest.mark.integration
@pytest.mark.parametrize("i", range(1, 7))
def test_single_clip_has_no_cut(i, media) -> None:
    """片段内部有运动（曼德博、生命游戏、渐变、色条），不能误报。"""
    cuts = detect_cuts(media / "montage" / f"clip{i}.mp4")
    assert len(cuts) <= 1, f"clip{i} 内部误报 {cuts}"
    assert cuts == [], f"clip{i} 内部误报 {cuts}"


@pytest.mark.integration
def test_cut_times_use_real_pts_on_vfr(tmp_path) -> None:
    """VFR：帧每 0.1s 出现一个（隔 3 帧抽一帧），黑→白的切点在 2.0s，必须用帧 pts 而不是 index/fps。"""
    out = tmp_path / "vfr_cut.mp4"
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=64x36:r=30:d=2",
        "-f", "lavfi", "-i", "color=c=white:s=64x36:r=30:d=2",
        "-filter_complex", "[0][1]concat=n=2:v=1:a=0,select='not(mod(n,3))'",
        "-fps_mode", "vfr", str(out),
    )
    cuts = detect_cuts(out)
    assert len(cuts) == 1 and cuts[0] == pytest.approx(2.0, abs=0.01)


@pytest.mark.integration
def test_cuts_respect_scale_and_threshold_args(media, tmp_path) -> None:
    video = _concat(media, ["clip1", "clip2"], tmp_path / "two.mp4")
    assert detect_cuts(video, scale=(32, 18)) and abs(detect_cuts(video, scale=(32, 18))[0] - 4.0) <= FRAME + 1e-6
    assert detect_cuts(video, threshold=5.0) == []  # 阈值高到不可能


@pytest.mark.integration
def test_cuts_on_video_without_video_stream_raises(media) -> None:
    with pytest.raises(MediaError, match="music_120bpm.wav"):
        detect_cuts(media / "montage" / "music_120bpm.wav")


@pytest.mark.integration
def test_cuts_on_missing_file_raises(tmp_path) -> None:
    with pytest.raises(MediaError, match="nope.mp4"):
        detect_cuts(tmp_path / "nope.mp4")
