"""probe：ffprobe 封装。"""

import pytest

from autocut.probe import AudioTrack, MediaError, MediaInfo, VideoInfo, parse_probe, probe
from conftest import run_ffmpeg

# ------------------------------------------------------------------ 单元：解析 ffprobe JSON


def _video_stream(**over) -> dict:
    s = {
        "index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
        "r_frame_rate": "30/1", "avg_frame_rate": "30/1", "display_aspect_ratio": "16:9",
    }
    s.update(over)
    return s


@pytest.mark.unit
def test_parse_audio_title_from_title_or_mov_name_tag() -> None:
    """mp4 里音轨标题常被 ffprobe 报成 tags.name 而不是 tags.title，两者都要认。"""
    data = {
        "format": {"duration": "10.5", "format_name": "mov,mp4"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 64, "height": 36,
             "r_frame_rate": "30/1", "avg_frame_rate": "30/1"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000",
             "duration": "10.4", "tags": {"title": "mix"}},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 1, "sample_rate": "44100",
             "tags": {"name": "mic"}},
            {"index": 3, "codec_type": "audio", "codec_name": "aac", "channels": 1, "sample_rate": "44100"},
        ],
    }
    info = parse_probe(data, "x.mp4")
    assert [a.track_number for a in info.audio] == [0, 1, 2]
    assert [a.stream_index for a in info.audio] == [1, 2, 3]
    assert [a.title for a in info.audio] == ["mix", "mic", None]
    assert info.audio[0].sample_rate == 48000 and info.audio[0].channels == 2
    assert info.audio[0].duration == pytest.approx(10.4)
    assert info.audio[1].duration == pytest.approx(10.5)  # 缺 duration 时回落到容器时长


@pytest.mark.unit
@pytest.mark.parametrize(
    "side_data, tags, expected",
    [
        ([], {}, 0),
        ([{"rotation": -90}], {}, 90),  # iPhone 竖拍：ffprobe 报 -90 = 显示时需顺时针转 90°
        ([{"rotation": 90}], {}, 270),
        ([{"rotation": 180}], {}, 180),
        ([], {"rotate": "90"}, 90),  # 老式 rotate tag（顺时针）
    ],
)
def test_parse_rotation_is_clockwise_degrees(side_data: list, tags: dict, expected: int) -> None:
    data = {
        "format": {"duration": "1", "format_name": "mov"},
        "streams": [_video_stream(side_data_list=side_data, tags=tags)],
    }
    v = parse_probe(data, "x.mp4").video
    assert v is not None and v.rotation == expected


@pytest.mark.unit
def test_parse_display_aspect_accounts_for_rotation_and_sar() -> None:
    base = {"format": {"duration": "1", "format_name": "mov"}}
    plain = parse_probe({**base, "streams": [_video_stream()]}, "x").video
    assert plain.display_aspect == pytest.approx(16 / 9)
    portrait = parse_probe({**base, "streams": [_video_stream(side_data_list=[{"rotation": -90}])]}, "x").video
    assert portrait.display_aspect == pytest.approx(9 / 16)
    no_dar = _video_stream(width=720, height=576, sample_aspect_ratio="16:15")
    no_dar.pop("display_aspect_ratio")
    anamorphic = parse_probe({**base, "streams": [no_dar]}, "x").video
    assert anamorphic.display_aspect == pytest.approx(720 * 16 / 15 / 576)


@pytest.mark.unit
def test_parse_without_video_or_audio() -> None:
    info = parse_probe({"format": {"duration": "3.0", "format_name": "wav"}, "streams": []}, "x.wav")
    assert info.video is None and info.audio == ()
    assert info.duration == pytest.approx(3.0)


@pytest.mark.unit
def test_parse_missing_format_raises() -> None:
    with pytest.raises(MediaError, match="x.mp4"):
        parse_probe({}, "x.mp4")


# ------------------------------------------------------------------ 集成：真实文件


@pytest.mark.integration
def test_probe_talk_zh(media, truth) -> None:
    info = probe(media / "talk_zh.mp4")
    assert isinstance(info, MediaInfo)
    assert abs(info.duration - truth("talk_zh")["duration"]) < 0.05
    v = info.video
    assert isinstance(v, VideoInfo)
    assert (v.width, v.height) == (640, 360)
    assert v.fps == pytest.approx(30.0)
    assert v.is_vfr is False
    assert v.codec == "h264" and v.rotation == 0
    assert v.display_aspect == pytest.approx(16 / 9, rel=1e-3)
    assert len(info.audio) == 1
    a = info.audio[0]
    assert isinstance(a, AudioTrack)
    assert (a.track_number, a.channels, a.sample_rate, a.codec) == (0, 1, 48000, "aac")
    assert "mp4" in info.format_name


@pytest.mark.integration
def test_probe_game_commentary_has_three_titled_tracks(media) -> None:
    info = probe(media / "game_commentary.mp4")
    assert [a.title for a in info.audio] == ["mix", "mic", "game"]
    assert [a.track_number for a in info.audio] == [0, 1, 2]
    assert [a.stream_index for a in info.audio] == [1, 2, 3]


@pytest.mark.integration
def test_probe_audio_only_file(media) -> None:
    info = probe(media / "montage" / "music_120bpm.wav")
    assert info.video is None
    assert info.duration == pytest.approx(24.0, abs=0.05)
    assert len(info.audio) == 1


@pytest.mark.integration
def test_probe_detects_vfr(tmp_path) -> None:
    """帧时间间隔不均匀（1,1,8 帧循环）的视频必须标成 VFR。"""
    out = tmp_path / "vfr.mp4"
    run_ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x36:r=30:d=4", "-vf", "select='lt(mod(n,10),3)'",
               "-fps_mode", "vfr", str(out))
    assert probe(out).video.is_vfr is True


@pytest.mark.integration
def test_probe_reads_rotation(media, tmp_path) -> None:
    out = tmp_path / "rot.mp4"
    run_ffmpeg("-display_rotation", "90", "-i", str(media / "montage" / "clip1.mp4"), "-c", "copy", str(out))
    v = probe(out).video
    assert v.rotation == 270  # ffmpeg 的 -display_rotation 是逆时针 90° = 显示时需顺时针转 270°
    assert v.display_aspect == pytest.approx(360 / 640, rel=1e-3)


@pytest.mark.integration
def test_probe_corrupt_file_raises_media_error_with_filename(tmp_path) -> None:
    bad = tmp_path / "broken_clip.mp4"
    bad.write_text("this is not a video")
    with pytest.raises(MediaError) as ei:
        probe(bad)
    msg = str(ei.value)
    assert "broken_clip.mp4" in msg
    assert "ffprobe" in msg  # 带上 ffprobe 的关键报错行
    assert "moov atom not found" in msg or "Invalid data" in msg


@pytest.mark.integration
def test_probe_missing_file_raises_media_error(tmp_path) -> None:
    with pytest.raises(MediaError, match="no_such_file.mp4"):
        probe(tmp_path / "no_such_file.mp4")
