"""导出：FCPXML / xmeml / EDL / SRT / 标记。重点是交接最容易坏的地方：路径、时间有理数、音视频联动、竖屏尺寸、多音轨、剪掉的片段还能找回。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from autocut.captions import parse_srt
from autocut.export import export
from autocut.plan import Bed, Output, Plan, Segment, Source
from autocut.render import frame_grid

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify  # noqa: E402
from test_render import _montage, talk_plan  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))


def secs(s: str) -> Fraction:
    s = s.strip()
    assert s.endswith("s"), s
    s = s[:-1]
    return Fraction(s) if "/" in s else Fraction(int(s))


def url_to_path(u: str) -> Path:
    p = urlparse(u)
    assert p.scheme == "file", u
    assert p.netloc in ("", "localhost"), u
    return Path(unquote(p.path))


def spine_clips(root: ET.Element) -> list[ET.Element]:
    return list(root.find(".//spine"))


# ------------------------------------------------------------------ FCPXML


def test_fcpxml_talk_structure_paths_and_duration(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path)
    p = export(plan, "fcpxml")
    assert p == tmp_path / "talk.main.fcpxml"
    root = ET.parse(p).getroot()
    assert root.tag == "fcpxml" and root.get("version") == "1.10"
    # asset：src 是绝对路径的 file:// URL 且文件存在（S33）
    assets = root.findall(".//asset")
    assert assets
    for a in assets:
        src = a.find("media-rep").get("src")
        assert src.startswith("file:///") and url_to_path(src).is_absolute() and url_to_path(src).exists()
    fd = secs(root.find(".//format").get("frameDuration"))
    assert fd == Fraction(1, 30)
    clips = spine_clips(root)
    kept = plan.outputs[0].kept()
    assert len(clips) == len(kept) and all(c.tag == "asset-clip" for c in clips)  # 音视频一个 asset-clip，联动（S15）
    # 时间线连续、总时长 = 方案输出时长 ±1 帧；所有时间都是整帧
    t = Fraction(0)
    for c in clips:
        assert secs(c.get("offset")) == t
        d = secs(c.get("duration"))
        assert (d / fd).denominator == 1 and (secs(c.get("start")) / fd).denominator == 1
        t += d
    assert abs(float(t) - plan.outputs[0].duration) <= 1 / 30 + 1e-6
    assert secs(root.find(".//sequence").get("duration")) == t
    # 剪掉的片段 → 剪口上的标记，写明素材里的原位置和理由
    cut = [s for s in plan.outputs[0].segments if s.action == "cut"]
    marks = root.findall(".//marker")
    assert len(marks) == len(cut) and all(m.get("value").startswith("剪掉") for m in marks)
    assert any("停顿" in m.get("note") for m in marks)
    res = verify.verify(MEDIA / "talk_zh.truth.json", MEDIA / "talk_zh.mp4", p, "export_nle")
    assert res["pass"], res["reasons"]


def test_fcpxml_cut_segments_as_disabled_clips_or_nothing(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path)
    n_cut = sum(1 for s in plan.outputs[0].segments if s.action == "cut")
    root = ET.parse(export(plan, "fcpxml", tmp_path / "d.fcpxml", cuts="disabled")).getroot()
    lane = [e for e in root.iter("asset-clip") if e.get("lane") == "1"]
    assert len(lane) == n_cut and all(e.get("enabled") == "0" and e.get("name").startswith("[剪掉]") for e in lane)
    assert len(spine_clips(root)) == len(plan.outputs[0].kept()) and not root.findall(".//marker")
    root = ET.parse(export(plan, "fcpxml", tmp_path / "n.fcpxml", cuts="none")).getroot()
    assert not root.findall(".//marker") and not [e for e in root.iter("asset-clip") if e.get("lane")]
    with pytest.raises(ValueError, match="cuts"):
        export(plan, "fcpxml", cuts="bogus")


def test_fcpxml_vertical_size_and_29_97_frame_rate(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=1.0, end=4.3), Segment(id="b", source="a", start=6.0, end=7.1)],
                     width=1080, height=1920, fps=29.97)
    root = ET.parse(export(plan, "fcpxml")).getroot()
    f1 = root.find(".//format[@id='r1']")
    assert (f1.get("width"), f1.get("height")) == ("1080", "1920")  # S10：竖屏不能变成 1280x720
    assert root.find(".//sequence").get("format") == "r1"
    fd = secs(f1.get("frameDuration"))
    assert fd == Fraction(1001, 30000)
    for c in spine_clips(root):
        for k in ("offset", "start", "duration"):
            assert (secs(c.get(k)) / fd).denominator == 1, (k, c.get(k))  # 29.97 下所有时间都是整帧，不会逐段累积误差（S18/S19）
    total = sum(secs(c.get("duration")) for c in spine_clips(root))
    assert abs(float(total) - plan.outputs[0].duration) <= float(fd) + 1e-6


def test_fcpxml_rotated_phone_clip_uses_display_size(tmp_path: Path) -> None:
    rot = tmp_path / "rot.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(MEDIA / "talk_zh.mp4"), "-t", "4", "-c", "copy", str(rot)], check=True)
    plan = Plan(recipe="manual", sources={"a": Source(id="a", path=str(rot), duration=4.0)}, path=tmp_path / "r.autocut.json",
                outputs=[Output(id="o", segments=[Segment(id="a", source="a", start=0.0, end=3.0)])])
    root = ET.parse(export(plan, "fcpxml")).getroot()
    fmts = {f.get("id"): (f.get("width"), f.get("height")) for f in root.findall(".//format")}
    assert fmts["r1"] == ("360", "640")  # 旋转后的显示尺寸
    assert fmts[root.find(".//asset").get("format")] == ("360", "640")


def test_fcpxml_multitrack_source_enables_only_the_selected_track(tmp_path: Path) -> None:
    t = json.loads((MEDIA / "game_commentary.truth.json").read_text())
    plan = Plan(recipe="manual", path=tmp_path / "g.autocut.json",
                sources={"a": Source(id="a", path=str(MEDIA / "game_commentary.mp4"), duration=t["duration"], audio_track=1)},
                outputs=[Output(id="o", segments=[Segment(id="a", source="a", start=1.0, end=5.0)])])
    root = ET.parse(export(plan, "fcpxml")).getroot()
    a = root.find(".//asset")
    assert a.get("audioSources") == "3" and a.get("hasAudio") == "1"
    chans = root.find(".//asset-clip").findall("audio-channel-source")
    assert [c.get("active") for c in chans] == ["0", "1", "0"]
    assert [c.get("srcCh") for c in chans] == ["1", "2", "3"] and "mic" in chans[1].get("role")


def test_fcpxml_speed_beds_and_images(tmp_path: Path) -> None:
    K = MEDIA / "knowledge"
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=7.5, speed=2.0), Segment(id="b", source="a", start=8.0, end=9.0, reverse=True)])
    plan.sources["i"] = Source(id="i", path=str(K / "img1.png"), kind="image")
    plan.sources["m"] = Source(id="m", path=str(MEDIA / "montage" / "music_120bpm.wav"), kind="audio", duration=24.0)
    o = plan.outputs[0]
    o.segments.append(Segment(id="img", source="i", start=0, end=0, slot=2.0, reason="image"))
    o.beds = [Bed(source="m", at=1.0, gain_db=-8.0, duck_db=12.0)]
    root = ET.parse(export(plan, "fcpxml")).getroot()
    clips = spine_clips(root)
    assert [c.tag for c in clips] == ["asset-clip", "asset-clip", "video"]
    tm = clips[0].find("timeMap")
    pts = tm.findall("timept")
    assert secs(pts[0].get("time")) == 0 and secs(pts[1].get("time")) == secs(clips[0].get("duration"))
    assert secs(pts[1].get("value")) == pytest.approx(secs(clips[0].get("duration")) * 2)  # 2 倍速：片段 2s 对应素材 4s
    rev = clips[1].find("timeMap").findall("timept")
    assert secs(rev[0].get("value")) > secs(rev[1].get("value"))  # 倒放：素材时间递减
    bed = [e for e in root.iter("asset-clip") if e.get("lane") == "-1"]
    assert len(bed) == 1 and bed[0].find("adjust-volume").get("amount") == "-8dB" and bed[0].get("audioRole") == "music"
    for a in root.findall(".//asset"):
        assert url_to_path(a.find("media-rep").get("src")).exists()


# ------------------------------------------------------------------ xmeml


def test_xmeml_talk_links_paths_and_duration(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path)
    p = export(plan, "xmeml")
    assert p.name == "talk.main.xml"
    root = ET.parse(p).getroot()
    assert root.tag == "xmeml"
    seq = root.find("sequence")
    tb, ntsc = int(seq.find("rate/timebase").text), seq.find("rate/ntsc").text
    assert (tb, ntsc) == (30, "FALSE")
    vitems = seq.findall("./media/video/track/clipitem")
    kept = plan.outputs[0].kept()
    assert len(vitems) == len(kept)
    end = 0
    for it in vitems:
        assert int(it.find("start").text) == end
        end = int(it.find("end").text)
        links = it.findall("link")
        assert {l.find("mediatype").text for l in links} == {"video", "audio"}  # 音视频联动
    assert abs(end / 30 - plan.outputs[0].duration) <= 1 / 30 + 1e-6
    assert int(seq.find("duration").text) == end
    files = root.findall(".//file")
    full = [f for f in files if f.find("pathurl") is not None]
    assert len(full) == 1 and url_to_path(full[0].find("pathurl").text.replace("file://localhost", "file://")).exists()
    assert len(root.findall("./sequence/media/audio/track")) == 1  # 单声道素材 → 一条音频轨，不会变成立体声拆开
    res = verify.verify(MEDIA / "talk_zh.truth.json", MEDIA / "talk_zh.mp4", p, "export_nle")
    assert res["pass"], res["reasons"]


def test_xmeml_vertical_size_multitrack_and_speed(tmp_path: Path) -> None:
    t = json.loads((MEDIA / "game_commentary.truth.json").read_text())
    plan = Plan(recipe="manual", path=tmp_path / "g.autocut.json",
                sources={"a": Source(id="a", path=str(MEDIA / "game_commentary.mp4"), duration=t["duration"], audio_track=2)},
                outputs=[Output(id="o", width=1080, height=1920, segments=[Segment(id="a", source="a", start=1.0, end=5.0, speed=2.0)])])
    root = ET.parse(export(plan, "xmeml")).getroot()
    sc = root.find("./sequence/media/video/format/samplecharacteristics")
    assert (sc.find("width").text, sc.find("height").text) == ("1080", "1920")
    ai = root.find("./sequence/media/audio/track/clipitem")
    assert ai.find("sourcetrack/trackindex").text == "3"  # 三条单声道音轨，第 3 条（game）
    assert root.find(".//file/media/audio/channelcount").text == "3"
    speed = root.find(".//clipitem/filter/effect[name='Time Remap']/parameter/value")
    assert speed is not None and float(speed.text) == 200.0


# ------------------------------------------------------------------ EDL


def test_edl_events_and_timecodes(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path)
    p = export(plan, "edl")
    text = p.read_text()
    assert text.startswith("TITLE:") and "FCM: NON-DROP FRAME" in text
    ev = re.findall(r"^(\d{3})\s+\S+\s+(\S+)\s+C\s+(\d\d:\d\d:\d\d:\d\d) (\d\d:\d\d:\d\d:\d\d) (\d\d:\d\d:\d\d:\d\d) (\d\d:\d\d:\d\d:\d\d)$", text, re.M)
    kept = plan.outputs[0].kept()
    assert len(ev) == len(kept) and [e[0] for e in ev] == [f"{i:03d}" for i in range(1, len(kept) + 1)]
    assert all(e[1] == "AA/V" for e in ev)
    assert text.count("* FROM CLIP NAME: talk_zh.mp4") == len(kept)

    def tc(s: str) -> int:
        h, m, sec, f = (int(x) for x in s.split(":"))
        return ((h * 60 + m) * 60 + sec) * 30 + f

    rec_end = 0
    for _, _, si, so, ri, ro in ev:
        assert tc(ri) == rec_end and tc(so) - tc(si) == tc(ro) - tc(ri)
        rec_end = tc(ro)
    assert abs(rec_end / 30 - plan.outputs[0].duration) <= 1 / 30 + 1e-6
    res = verify.verify(MEDIA / "talk_zh.truth.json", MEDIA / "talk_zh.mp4", p, "export_nle")
    assert res["pass"] and res["events"] == len(kept), res["reasons"]


def test_edl_speed_change_line(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=3.5, end=5.5, speed=2.0)])
    text = export(plan, "edl").read_text()
    assert re.search(r"^M2\s+AX\s+060\.0\s+\d\d:\d\d:\d\d:\d\d$", text, re.M)
    ev = re.search(r"C\s+(\d\d:\d\d:\d\d:\d\d) (\d\d:\d\d:\d\d:\d\d) (\d\d:\d\d:\d\d:\d\d) (\d\d:\d\d:\d\d:\d\d)", text)
    assert ev.groups() == ("00:00:03:15", "00:00:05:15", "00:00:00:00", "00:00:01:00")  # 素材 2s → 时间线 1s


# ------------------------------------------------------------------ SRT 与标记


def test_srt_export_follows_the_cuts(tmp_path: Path) -> None:
    from autocut.plan import Captions

    t = json.loads((MEDIA / "talk_zh.truth.json").read_text())
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0.0, end=2.8108), Segment(id="b", source="a", start=11.1534, end=14.1093)],
                     captions=Captions(enabled=True))
    cues = parse_srt(export(plan, "srt").read_text(encoding="utf-8"))
    assert [c.text for c in cues] == ["大家好 欢迎回到我的频道", "首先你需要准备好原始素材"]
    assert cues[1].start == pytest.approx(2.8108, abs=0.05)  # 第二句从素材 11.15s 提前到紧接着第一句
    assert t["segments"][4]["text"].startswith("首先")


def test_srt_export_without_words_raises_with_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0.0, end=2.0)])
    plan.sources["a"].words = None
    monkeypatch.setattr("autocut.analysis.Media.words", lambda self, *a, **k: None)
    with pytest.raises(ValueError, match="逐词文字稿"):
        export(plan, "srt")


def test_markers_contain_montage_beats(tmp_path: Path) -> None:
    slots = [2.5, 2.5, 3.0, 2.0, 2.5, 2.5]
    plan = _montage(slots, 12.0, "m")
    plan.path = tmp_path / "mont.autocut.json"
    p = export(plan, "markers")
    assert p.name == "mont.m.markers.fcpxml"
    root = ET.parse(p).getroot()
    clips = spine_clips(root)
    # 标记的时间线位置 = 所在片段 offset + (marker.start - clip.start)
    times: list[tuple[float, str]] = []
    for c in clips:
        for m in c.findall("marker"):
            times.append((float(secs(c.get("offset")) + secs(m.get("start")) - secs(c.get("start"))), m.get("value")))
    truth = json.loads((MEDIA / "montage.truth.json").read_text())
    got = [t for t, _ in times]
    # 音乐的拍点（0.5s 一拍，输出前 15s 共 30 拍）：每一拍都有标记，误差 ≤ 1 帧
    for b in truth["beats"]:
        if b < 15.0:
            assert min(abs(g - b) for g in got) <= 1 / 30 + 1e-6, f"拍点 {b}s 没有标记"
    assert sum(1 for _, v in times if v.startswith(("拍", "小节"))) >= 25
    # 卡点片段的起点也有标记
    starts = [f0 / 30 for _, f0, _ in frame_grid(plan.outputs[0], Fraction(30))]
    for s in starts:
        assert any(abs(g - s) <= 1 / 30 + 1e-6 and v.startswith("卡点") for g, v in times), f"片段起点 {s}s 没有卡点标记"
    assert any(v.startswith("小节") for _, v in times)


def test_unknown_format_and_missing_source_are_explained(tmp_path: Path) -> None:
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=0.0, end=2.0)])
    with pytest.raises(ValueError, match="fcpxml"):
        export(plan, "mp3")
    plan.sources["a"].path = str(tmp_path / "gone.mp4")
    with pytest.raises(Exception, match="gone.mp4"):
        export(plan, "fcpxml")
