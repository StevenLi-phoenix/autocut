"""出片：时长/音画同步、剪口无爆音、slot/speed/mute/pitch、卡点混剪 + BGM 闪避、图片 + Ken Burns + 配音、竖屏 reframe、
旋转与可变帧率、字幕烧录、dry_run、进度回调。素材都是 fixtures/media 里的短片段。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from autocut.plan import Bed, Captions, Output, Overlay, Plan, Reframe, Segment, Source
from autocut.render import RenderError, RenderResult, frame_grid, render, snap_fps
from fractions import Fraction

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify  # noqa: E402
from test_captions import words_from_truth  # noqa: E402

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ 助手


def ffprobe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def decode_audio(path: Path, sr: int = 48000) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    assert r.returncode == 0, r.stderr
    return np.frombuffer(r.stdout, dtype="<f4")


def gray_frame(path: Path, t: float, size: tuple[int, int] | None = None) -> np.ndarray:
    vf = "format=gray" if size is None else f"scale={size[0]}:{size[1]},format=gray"
    w, h = size or (0, 0)
    if size is None:
        st = ffprobe(path)["streams"][0]
        w, h = st["width"], st["height"]
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.uint8).reshape(h, w).astype(float)


def rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-9))


def talk_plan(tmp_path: Path, segs: list[Segment] | None = None, **out_kw) -> Plan:
    t = json.loads((MEDIA / "talk_zh.truth.json").read_text())
    if segs is None:
        segs, n = [], 0
        for s in t["segments"]:
            n += 1
            if s["kind"] == "speech":
                segs.append(Segment(id=f"s{n:03d}", source="a", start=s["start"], end=s["end"], reason="speech"))
            elif s["end"] - s["start"] > 0.4:
                segs.append(Segment(id=f"s{n:03d}", source="a", start=s["start"], end=s["start"] + 0.4, reason="pause"))
                segs.append(Segment(id=f"s{n:03d}x", source="a", start=s["start"] + 0.4, end=s["end"], reason="silence", action="cut",
                                    detail=f"停顿 {s['end'] - s['start']:.1f}s → 保留 0.4s"))
            else:
                segs.append(Segment(id=f"s{n:03d}", source="a", start=s["start"], end=s["end"], reason="pause"))
    (tmp_path / "words.json").write_text(json.dumps(words_from_truth(t), ensure_ascii=False))
    return Plan(recipe="manual", sources={"a": Source(id="a", path=str(MEDIA / "talk_zh.mp4"), duration=t["duration"], words=str(tmp_path / "words.json"))},
                outputs=[Output(id="main", segments=segs, **out_kw)], path=tmp_path / "talk.autocut.json")


def one(res: list[RenderResult]) -> RenderResult:
    assert len(res) == 1
    return res[0]


def assert_sync(r: RenderResult, fps: float = 30.0) -> None:
    frame = 1.0 / fps
    assert r.duration is not None and r.av_drift is not None
    assert abs(r.duration - r.expected_duration) <= frame + 1e-3, (r.duration, r.expected_duration)
    assert r.av_drift <= frame + 1e-3, r.av_drift


# ------------------------------------------------------------------ 单元：帧率与帧格


@pytest.mark.unit
def test_snap_fps_and_frame_grid_do_not_accumulate_error() -> None:
    assert snap_fps(29.97) == Fraction(30000, 1001)
    assert snap_fps(30.0) == 30 and snap_fps(59.94) == Fraction(60000, 1001) and snap_fps(23.976) == Fraction(24000, 1001)
    assert snap_fps(30.05) == 30 and snap_fps(29.9) == Fraction(30000, 1001)  # 可变帧率素材的平均帧率也吸附到标准值
    # 200 段每段 0.1234s：累计取整，总帧数与总时长的误差 ≤ 半帧（逐段取整会差几十帧）
    segs = [Segment(id=f"s{i}", source="a", start=0, end=0.1234) for i in range(200)]
    grid = frame_grid(Output(id="o", segments=segs), Fraction(30))
    assert grid[-1][2] == round(200 * 0.1234 * 30)
    assert all(f1 > f0 for _, f0, f1 in grid)


# ------------------------------------------------------------------ 口播：长停顿缩短


def test_talk_long_pauses_pass_verify_and_stay_in_sync(tmp_path: Path, truth) -> None:
    plan = talk_plan(tmp_path)
    r = one(render(plan))
    assert r.path == tmp_path / "talk.main.mp4" and r.path.exists()
    assert_sync(r)
    assert not r.warnings, r.warnings
    res = verify.verify(MEDIA / "talk_zh.truth.json", MEDIA / "talk_zh.mp4", r.path, "long_pauses")
    assert res["pass"], res["reasons"]
    assert res["checks"]["no_dead_air"] and res["checks"]["all_speech_intact"] and res["checks"]["no_clipped_speech"]
    info = ffprobe(r.path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (640, 360, "30/1")
    assert "autocut render" in info["format"]["tags"]["comment"]


def test_cut_seams_have_no_pop(tmp_path: Path) -> None:
    """在一句话中间切 3 刀：有淡入淡出时，剪口 ±1ms 内的最大样本跳变极小；关掉淡入淡出才会爆音（对照）。"""
    jumps = {}
    for fade in (12.0, 0.0):
        segs = [Segment(id=f"s{i}", source="a", start=a, end=a + 0.5) for i, a in enumerate((3.5, 6.0, 7.0))]
        plan = talk_plan(tmp_path, segs, audio_fade_ms=fade)
        x = decode_audio(one(render(plan)).path)
        d = np.abs(np.diff(x))
        jumps[fade] = max(float(d[int(s * 48000) - 48 : int(s * 48000) + 48].max()) for s in (0.5, 1.0))
    assert jumps[12.0] < 0.02, jumps
    assert jumps[0.0] > 0.08 and jumps[0.0] > 5 * jumps[12.0], jumps  # 对照：不淡入淡出会有明显的跳变


def test_contiguous_kept_segments_are_not_faded_at_the_seam(tmp_path: Path) -> None:
    """素材里首尾相接的两个保留片段不加淡入淡出，接缝处电平不能凹下去。"""
    segs = [Segment(id="a", source="a", start=3.5, end=4.5), Segment(id="b", source="a", start=4.5, end=5.5)]
    r = one(render(talk_plan(tmp_path, segs)))
    ref = tmp_path / "ref.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "3.5", "-t", "2", "-i", str(MEDIA / "talk_zh.mp4"), "-vn", "-ac", "1", str(ref)], check=True)
    x, y = decode_audio(r.path), decode_audio(ref)
    seam = slice(int(0.98 * 48000), int(1.02 * 48000))
    assert rms_db(x[seam]) > rms_db(y[seam]) - 1.5


# ------------------------------------------------------------------ slot / speed / mute / pitch


def test_slot_pads_with_frozen_frame_and_silence(tmp_path: Path) -> None:
    segs = [Segment(id="a", source="a", start=3.5, end=4.5, slot=2.5), Segment(id="b", source="a", start=6.0, end=7.0)]
    r = one(render(talk_plan(tmp_path, segs)))
    assert r.expected_duration == pytest.approx(3.5)
    assert_sync(r)
    x = decode_audio(r.path)
    assert rms_db(x[int(1.2 * 48000) : int(2.4 * 48000)]) < -60  # 补齐部分是静音
    assert rms_db(x[int(0.1 * 48000) : int(0.9 * 48000)]) > -40
    # 定格：补齐的那段画面不再变化（素材是逐帧动画，不定格相邻帧一定不同）
    f1, f2 = gray_frame(r.path, 1.6), gray_frame(r.path, 2.3)
    assert np.abs(f1 - f2).mean() < 0.5
    assert np.abs(gray_frame(r.path, 0.3) - gray_frame(r.path, 0.6)).mean() > 1.0


def test_slot_shorter_than_content_truncates(tmp_path: Path) -> None:
    r = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=6.5, slot=1.0)])))
    assert r.expected_duration == pytest.approx(1.0)
    assert_sync(r)


def test_speed_two_halves_duration_and_keeps_pitch(tmp_path: Path) -> None:
    base = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=7.5)], ), out_dir=tmp_path / "b"))
    fast = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=7.5, speed=2.0)]), out_dir=tmp_path / "f"))
    assert fast.expected_duration == pytest.approx(2.0)
    assert_sync(fast)
    assert base.duration == pytest.approx(4.0, abs=0.04)

    def centroid(x: np.ndarray) -> float:
        sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        return float((sp * np.fft.rfftfreq(len(x), 1 / 48000)).sum() / sp.sum())

    cb, cf = centroid(decode_audio(base.path)[: 4 * 48000]), centroid(decode_audio(fast.path)[: 2 * 48000])
    assert 0.8 < cf / cb < 1.35  # atempo 变速不变调（直接加速采样会让质心翻倍）


def test_speed_beyond_atempo_range_is_chained(tmp_path: Path) -> None:
    r = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=5.5, speed=0.25)])))
    assert r.expected_duration == pytest.approx(8.0)
    assert_sync(r)
    assert r.filter_graph.count("atempo=0.5") == 2


def test_mute_gives_silence_but_keeps_video(tmp_path: Path) -> None:
    segs = [Segment(id="a", source="a", start=3.5, end=4.5), Segment(id="b", source="a", start=6.0, end=7.0, mute=True)]
    r = one(render(talk_plan(tmp_path, segs)))
    assert_sync(r)
    x = decode_audio(r.path)
    assert rms_db(x[int(1.1 * 48000) : int(1.9 * 48000)]) < -70
    assert rms_db(x[int(0.1 * 48000) : int(0.9 * 48000)]) > -40
    assert np.abs(gray_frame(r.path, 1.2) - gray_frame(r.path, 1.5)).mean() > 1.0  # 画面照常


def test_pitch_plus_three_semitones_raises_frequency_not_duration(tmp_path: Path) -> None:
    base = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=6.5)]), out_dir=tmp_path / "b"))
    up = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=6.5, pitch=3.0)]), out_dir=tmp_path / "u"))
    assert_sync(up)
    assert up.duration == pytest.approx(base.duration, abs=0.04)

    def centroid(x: np.ndarray) -> float:
        x = x[: 3 * 48000]
        sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        return float((sp * np.fft.rfftfreq(len(x), 1 / 48000)).sum() / sp.sum())

    ratio = centroid(decode_audio(up.path)) / centroid(decode_audio(base.path))
    assert 1.08 < ratio < 1.45, ratio  # 理论值 2^(3/12)=1.19


def test_reverse_plays_backwards(tmp_path: Path) -> None:
    r = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.0, end=4.0, reverse=True)])))
    assert_sync(r)
    # 画面里有逐帧递增的时间码：倒放时第一帧是素材 4.0s 附近、最后一帧是 3.0s 附近，用整帧差判断顺序
    fwd = one(render(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.0, end=4.0)]), out_dir=tmp_path / "f"))
    a0, b0 = gray_frame(fwd.path, 0.0), gray_frame(fwd.path, 0.95)
    a1, b1 = gray_frame(r.path, 0.0), gray_frame(r.path, 0.95)
    assert np.abs(a1 - b0).mean() < np.abs(a1 - a0).mean() and np.abs(b1 - a0).mean() < np.abs(b1 - b0).mean()


# ------------------------------------------------------------------ 卡点混剪 + BGM


def _montage(slots: list[float], duck: float, out_id: str) -> Plan:
    M = MEDIA / "montage"
    srcs = {f"c{i}": Source(id=f"c{i}", path=str(M / f"clip{i}.mp4"), duration=4.0) for i in range(1, 7)}
    srcs["m"] = Source(id="m", path=str(M / "music_120bpm.wav"), kind="audio", duration=24.0)
    segs = [Segment(id=f"b{i + 1:03d}", source=f"c{i + 1}", start=0.5, end=3.0, reason="beat", slot=s) for i, s in enumerate(slots)]
    return Plan(recipe="montage", sources=srcs, outputs=[Output(id=out_id, segments=segs, beds=[Bed(source="m", at=0.0, gain_db=-3.0, duck_db=duck)])])


def test_montage_six_clips_with_bgm_and_duck(tmp_path: Path) -> None:
    slots = [2.5, 2.5, 3.0, 2.0, 2.5, 2.5]  # 15s，全落在 0.5s 的拍点上
    plain, ducked = _montage(slots, 0.0, "plain"), _montage(slots, 12.0, "duck")
    for p in (plain, ducked):
        p.path = tmp_path / "mont.autocut.json"
    rp, rd = one(render(plain)), one(render(ducked))
    for r in (rp, rd):
        assert r.expected_duration == pytest.approx(15.0)
        assert_sync(r)
        assert not r.warnings, r.warnings
    # 画面切点在拍点上：每个片段起点 = 拍点（0.5s 的整数倍）
    assert [f0 / 30 for _, f0, _ in frame_grid(plain.outputs[0], Fraction(30))] == pytest.approx([0, 2.5, 5.0, 8.0, 10.0, 12.5])
    # BGM 在：整体电平远高于片段自带的 -40dB 底噪
    xp, xd = decode_audio(rp.path), decode_audio(rd.path)
    assert rms_db(xp) > -30
    # ducking 生效：片段音频有声时 BGM 被压低（整体电平明显下降）
    assert rms_db(xd) < rms_db(xp) - 2.0, (rms_db(xd), rms_db(xp))
    assert abs(len(xp) / 48000 - 15.0) < 0.05


def test_duck_lowers_bgm_under_speech_only(tmp_path: Path) -> None:
    """口播 + BGM：人声段里 BGM 被压低，停顿里（只有底噪）基本不受影响。
    有闪避和无闪避的两版其余完全相同，所以 d = x(无闪避) - x(闪避) = BGM 被压掉的那部分；
    BGM 原电平从素材直接量，压低量 D 满足 rms(d) = rms(BGM)·(1 - 10^(-D/20))。"""
    segs = [(3.2, 9.2), (9.3, 10.6)]  # 说话 6s + 停顿 1.3s（9.25–11.15 是静音）
    x: dict[float, np.ndarray] = {}
    for duck in (0.0, 14.0):
        plan = talk_plan(tmp_path, [Segment(id=f"s{i}", source="a", start=a, end=b) for i, (a, b) in enumerate(segs)])
        o = plan.outputs[0]
        o.id = f"d{duck}"
        plan.sources["m"] = Source(id="m", path=str(MEDIA / "montage" / "music_120bpm.wav"), kind="audio", duration=24.0)
        o.beds = [Bed(source="m", at=0.0, gain_db=-6.0, duck_db=duck)]
        x[duck] = decode_audio(one(render(plan)).path)
    n = min(len(v) for v in x.values())
    d = x[0.0][:n] - x[14.0][:n]
    bgm = decode_audio(MEDIA / "montage" / "music_120bpm.wav") * 10 ** (-6 / 20)
    sp = slice(int(1.0 * 48000), int(5.0 * 48000))  # 说话中
    pa = slice(int(6.9 * 48000), int(7.3 * 48000))  # 停顿里，离说话结束 0.9s 以上，压缩器已基本释放

    def drop_db(sl: slice) -> float:
        frac = min(rms_lin(d[sl]) / rms_lin(bgm[sl]), 0.999)
        return float(-20 * np.log10(1 - frac))

    assert drop_db(sp) > 8.0, drop_db(sp)
    assert drop_db(pa) < drop_db(sp) - 5.0, (drop_db(sp), drop_db(pa))  # 说完话后 BGM 恢复


def rms_lin(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


# ------------------------------------------------------------------ 图片 + Ken Burns + 配音


def test_images_kenburns_and_narration_bed(tmp_path: Path) -> None:
    K = MEDIA / "knowledge"
    srcs = {f"i{i}": Source(id=f"i{i}", path=str(K / f"img{i}.png"), kind="image") for i in (1, 2, 3)}
    srcs["n"] = Source(id="n", path=str(MEDIA / "talk_zh.mp4"), kind="audio", duration=40.7)  # 拿口播当配音
    segs = [Segment(id=f"i{i}", source=f"i{i}", start=0, end=0, reason="image", slot=4.0) for i in (1, 2, 3)]
    segs[1].evidence["motion"] = "none"
    ov = [Overlay(kind="text", at=0.5, duration=2.0, text="为什么天空是蓝色的？", position="top"),
          Overlay(kind="image", at=5.0, duration=2.0, source="i3", position="bottom", motion="kenburns")]
    plan = Plan(recipe="narrate", sources=srcs, path=tmp_path / "k.autocut.json",
                outputs=[Output(id="o", segments=segs, overlays=ov, beds=[Bed(source="n", at=0.0, start=3.2, end=15.2, role="narration")])])
    r = one(render(plan))
    assert r.expected_duration == pytest.approx(12.0)
    assert_sync(r)
    assert not r.warnings, r.warnings
    assert rms_db(decode_audio(r.path)) > -40  # 配音在
    # 第 1 段 Ken Burns：开头和结尾的画面不同（缩放了）；第 2 段 motion=none：纯静止
    assert np.abs(gray_frame(r.path, 0.1) - gray_frame(r.path, 3.8)).mean() > 0.3
    assert np.abs(gray_frame(r.path, 4.4) - gray_frame(r.path, 7.5)).mean() < 0.5  # 4–8s 静止（叠加层只在 5–7s）
    assert np.abs(gray_frame(r.path, 8.3) - gray_frame(r.path, 11.5)).mean() > 0.3
    # 文字叠加层：0.5–2.5s 顶部有文字，3s 以后没有
    top = lambda t: gray_frame(r.path, t)[: 120]  # noqa: E731
    assert np.abs(top(1.5) - top(3.3)).mean() > 0.5


# ------------------------------------------------------------------ 画幅、旋转、帧率


def test_vertical_reframe_output_size_and_default_loudnorm(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=3.2, end=9.2)], width=720, height=1280, reframe=Reframe(mode="center"))
    r = one(render(plan))
    v = next(s for s in ffprobe(r.path)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (720, 1280)
    assert_sync(r)
    assert r.loudness is not None and r.loudness["target"] == -14.0  # 竖屏默认 -14 LUFS
    out = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(r.path), "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True).stderr
    import re

    lufs = float(re.findall(r"I:\s+(-?\d+\.\d+) LUFS", out)[-1])
    peak = float(re.findall(r"Peak:\s+(-?\d+\.\d+) dBFS", out)[-1])
    assert abs(lufs + 14.0) < 1.0 and peak <= -1.0, (lufs, peak)
    # 居中裁切：画面中心的内容来自素材中心（和直接 crop 对比）
    ref = tmp_path / "ref.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(r.path), "-frames:v", "1", str(ref)], check=True)
    assert ref.exists()


def test_reframe_keyframes_follow_the_subject(tmp_path: Path) -> None:
    """center 之外：按 keyframes 插值移动裁切框。keyframe 在最左 → 画面取素材最左边。"""
    left = talk_plan(tmp_path, [Segment(id="a", source="a", start=3.2, end=5.2)], width=360, height=640,
                     reframe=Reframe(mode="track", keyframes=[[0.0, 0.0, 0.5], [2.0, 1.0, 0.5]]))
    r = one(render(left, loudnorm=False))
    a, b = gray_frame(r.path, 0.1), gray_frame(r.path, 1.9)
    assert np.abs(a - b).mean() > 5  # 裁切框从最左移到最右，两端画面明显不同
    assert "crop=" in r.filter_graph and "lt(" in r.filter_graph


def test_rotated_phone_video_is_not_squashed(tmp_path: Path) -> None:
    """手机竖拍：横向存储 + 旋转元数据。输出必须是竖屏、内容不被拉扁（S37）。"""
    rot = tmp_path / "rot.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(MEDIA / "talk_zh.mp4"), "-t", "4", "-c", "copy", str(rot)], check=True)
    plan = Plan(recipe="manual", sources={"a": Source(id="a", path=str(rot), duration=4.0)}, path=tmp_path / "r.autocut.json",
                outputs=[Output(id="o", segments=[Segment(id="a", source="a", start=0.0, end=3.0)])])
    r = one(render(plan, loudnorm=False))
    v = next(s for s in ffprobe(r.path)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (360, 640), "旋转后的显示尺寸应是 360x640"
    # 参照：ffmpeg 自己转正的同一时刻画面
    ref = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", str(rot), "-frames:v", "1", "-vf", "scale=360:640,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    ref = np.frombuffer(ref, dtype=np.uint8).reshape(640, 360).astype(float)
    got = gray_frame(r.path, 1.0)
    squashed = subprocess.run(["ffmpeg", "-v", "error", "-noautorotate", "-ss", "1.0", "-i", str(rot), "-frames:v", "1", "-vf", "scale=360:640,format=gray", "-f", "rawvideo", "-"],
                              capture_output=True).stdout
    squashed = np.frombuffer(squashed, dtype=np.uint8).reshape(640, 360).astype(float)
    assert np.abs(got - ref).mean() < np.abs(got - squashed).mean() / 3


def test_variable_frame_rate_source_becomes_constant(tmp_path: Path) -> None:
    vfr = tmp_path / "vfr.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(MEDIA / "talk_zh.mp4"), "-t", "6", "-vf", "select='lt(mod(n,7),5)'", "-fps_mode", "vfr",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(vfr)], check=True)
    from autocut.probe import probe

    if not probe(vfr).video.is_vfr:
        pytest.skip("没能造出可变帧率素材")
    plan = Plan(recipe="manual", sources={"a": Source(id="a", path=str(vfr), duration=6.0)}, path=tmp_path / "v.autocut.json",
                outputs=[Output(id="o", segments=[Segment(id="a", source="a", start=0.5, end=4.5)])])
    r = one(render(plan))
    assert any("可变帧率" in w for w in r.warnings), r.warnings
    v = next(s for s in ffprobe(r.path)["streams"] if s["codec_type"] == "video")
    assert v["r_frame_rate"] == v["avg_frame_rate"]
    assert int(v["nb_frames"]) == round(r.expected_duration * eval(v["r_frame_rate"]))
    assert r.av_drift is not None and r.av_drift < 0.05


# ------------------------------------------------------------------ 字幕、dry_run、进度、错误


def test_captions_sidecar_and_burn(tmp_path: Path) -> None:
    segs = [Segment(id="a", source="a", start=0.0, end=2.8108), Segment(id="b", source="a", start=3.1608, end=6.0)]
    plan = talk_plan(tmp_path, segs, captions=Captions(enabled=True, burn=False))
    plain = one(render(plan))
    assert plain.srt_path is not None and plain.srt_path.exists() and plain.srt_path.name == "talk.main.srt"
    assert "大家好" in plain.srt_path.read_text(encoding="utf-8")
    plan.outputs[0].id = "burned"
    burned = one(render(plan, burn_captions=True))
    assert burned.srt_path is not None and "ass=" in burned.filter_graph
    # 字幕区域（底部 1/3）的画面与不烧录的版本不同；顶部不受影响
    a, b = gray_frame(plain.path, 1.0), gray_frame(burned.path, 1.0)
    h = a.shape[0]
    assert np.abs(a[2 * h // 3 :] - b[2 * h // 3 :]).mean() > 0.5
    assert np.abs(a[: h // 3] - b[: h // 3]).mean() < 0.5
    # 参数覆盖：burn_captions=False 压过方案里的 burn=True
    plan.outputs[0].captions.burn = True
    plan.outputs[0].id = "forced_off"
    off = one(render(plan, burn_captions=False))
    assert "ass=" not in off.filter_graph


def test_captions_enabled_without_words_warns(tmp_path: Path, monkeypatch) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0, end=2.0)], captions=Captions(enabled=True, burn=True))
    plan.sources["a"].words = None
    monkeypatch.setattr("autocut.analysis.Media.words", lambda self, *a, **k: None)
    r = one(render(plan))
    assert any("没有可用的文字稿" in w for w in r.warnings), r.warnings
    assert r.srt_path is None


def test_dry_run_creates_no_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    plan = talk_plan(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    r = one(render(plan, out_dir=out_dir, dry_run=True))
    assert not out_dir.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert r.duration is None and r.av_drift is None
    assert r.command[0] == "ffmpeg" and "-/filter_complex" in r.command and str(out_dir / "talk.main.mp4") == r.command[-1]
    assert "concat=n=" in r.filter_graph and r.expected_duration > 0


def test_progress_reports_stages(tmp_path: Path) -> None:
    seen: list[tuple[str, float]] = []
    render(talk_plan(tmp_path, [Segment(id="a", source="a", start=0, end=6.0)], width=360, height=640), progress=lambda s, f: seen.append((s, f)))
    stages = [s for s, _ in seen]
    assert stages[0] == "prepare" and stages[-1] == "done" and "encode" in stages and "measure" in stages
    enc = [f for s, f in seen if s == "encode"]
    assert enc == sorted(enc) and enc[-1] >= 0.9


def test_selected_outputs_and_out_dir(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0, end=1.0)])
    plan.outputs.append(Output(id="skip", segments=[Segment(id="b", source="a", start=1.0, end=2.0)], selected=False))
    plan.outputs.append(Output(id="third", segments=[Segment(id="c", source="a", start=2.0, end=3.0)]))
    res = render(plan, out_dir=tmp_path / "o")
    assert [r.output_id for r in res] == ["main", "third"] and all(r.path.parent == tmp_path / "o" for r in res)
    only = render(plan, output_ids=["skip"], out_dir=tmp_path / "o2")
    assert [r.output_id for r in only] == ["skip"] and only[0].path.name == "talk.skip.mp4"


def test_errors_say_what_and_how_to_fix(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0, end=1.0, action="cut")])
    with pytest.raises(RenderError, match="没有保留的片段"):
        render(plan)
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0, end=1.0)])
    plan.sources["a"].path = str(tmp_path / "nope.mp4")
    with pytest.raises(RenderError, match="素材文件不存在.*nope.mp4"):
        render(plan)
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0, end=1.0)])
    plan.sources["a"].audio_track = 5
    with pytest.raises(RenderError, match="audio_track=5"):
        render(plan)


def _peak_ncc(a: np.ndarray, b: np.ndarray) -> float:
    """b 在 a 里滑动的归一化互相关峰值（FFT），都是 float64。"""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    num = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)[: len(a) - len(b) + 1]
    cs = np.concatenate([[0.0], np.cumsum(a * a)])
    win = np.maximum(cs[len(b) :] - cs[: -len(b)], 1e-12)
    return float((num / (np.sqrt(win) * np.sqrt(b @ b))).max())


def test_multitrack_source_uses_selected_audio_track(tmp_path: Path) -> None:
    """游戏直播：mix / mic / game 三条音轨，方案选 mic（音轨 1）→ 成片的声音来自 mic，不是 game。"""
    t = json.loads((MEDIA / "game_commentary.truth.json").read_text())
    s0 = next(s for s in t["segments"] if s["kind"] == "speech")
    out = {}
    for track in (1, 2):
        src = Source(id="a", path=str(MEDIA / "game_commentary.mp4"), duration=t["duration"], audio_track=track)
        plan = Plan(recipe="manual", sources={"a": src}, path=tmp_path / "g.autocut.json",
                    outputs=[Output(id=f"t{track}", segments=[Segment(id="a", source="a", start=s0["start"], end=s0["end"])])])
        r = one(render(plan, loudnorm=False))
        assert_sync(r)
        out[track] = decode_audio(r.path, 16000).astype(np.float64)

    def src_track(k: int) -> np.ndarray:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(s0["start"] + 0.2), "-t", "1.5", "-i", str(MEDIA / "game_commentary.mp4"), "-map", f"0:a:{k}",
                            "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True)
        return np.frombuffer(r.stdout, dtype="<f4").astype(np.float64)

    mic, game = src_track(1), src_track(2)
    assert _peak_ncc(out[1], mic) > 0.9 and _peak_ncc(out[2], game) > 0.9
    assert _peak_ncc(out[1], game) < 0.5 and _peak_ncc(out[2], mic) < 0.5
