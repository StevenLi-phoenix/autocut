"""evals/verify.py 自测：用 ffmpeg 按 truth 构造各种产物，确认校验器判得对。

产物全部生成在 pytest 的 tmp_path 里。素材在 fixtures/media/（gitignored，由
fixtures/make_fixtures.py 生成），缺失时整模块跳过。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"
sys.path.insert(0, str(ROOT / "evals"))

import verify  # noqa: E402

if not (MEDIA / "talk_zh.mp4").exists() or not (MEDIA / "lecture_en.mp4").exists():
    pytest.skip("fixtures/media 缺失：先跑 fixtures/make_fixtures.py", allow_module_level=True)

FPS = 30
GAP_KEEP = 0.3


# ---------------------------------------------------------------- 构造产物的助手


def truth_of(name: str) -> dict:
    return verify.load_truth(MEDIA / f"{name}.truth.json")


def ideal_ranges(truth: dict, drop_tagged: bool = False) -> list[tuple[float, float]]:
    """理想剪辑的源区间：保留语音，>1.0s 的停顿只留开头 0.3s，短停顿原样保留。
    drop_tagged：把 retake / filler 连同它们后面的停顿一起剪掉。"""
    out: list[list[float]] = []
    prev_kept = False
    for s in truth["segments"]:
        if s["kind"] == "speech":
            prev_kept = not (drop_tagged and s.get("tag"))
            if not prev_kept:
                continue
            a, b = s["start"], s["end"]
        else:
            if not prev_kept:
                continue
            a, b = s["start"], s["end"]
            if b - a > verify.DEAD_AIR_S:
                b = a + GAP_KEEP
        if out and abs(out[-1][1] - a) < 1e-6:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def render(src: Path, dst: Path, ranges: list[tuple[float, float]]) -> Path:
    """按源区间拼接成 mp4（重编码，保证样本级精度）。"""
    parts, labels = [], ""
    for i, (a, b) in enumerate(ranges):
        parts.append(f"[0:v]trim={a:.4f}:{b:.4f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim={a:.4f}:{b:.4f},asetpts=PTS-STARTPTS[a{i}]")
        labels += f"[v{i}][a{i}]"
    fc = ";".join(parts) + f";{labels}concat=n={len(ranges)}:v=1:a=1[v][a]"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", fc,
                        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", str(dst)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return dst


def tc(sec: float) -> str:
    f = round(sec * FPS)
    return f"{f // (FPS * 3600):02d}:{f // (FPS * 60) % 60:02d}:{f // FPS % 60:02d}:{f % FPS:02d}"


def write_fcpxml(dst: Path, src_name: str, ranges: list[tuple[float, float]]) -> Path:
    clips, off = [], 0.0
    for a, b in ranges:
        fa, fb, fo = round(a * FPS), round(b * FPS), round(off * FPS)
        clips.append(f'<asset-clip ref="r2" name="{src_name}" offset="{fo}/{FPS}s" start="{fa}/{FPS}s" '
                     f'duration="{fb - fa}/{FPS}s"/>')
        off += (fb - fa) / FPS
    dst.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10"><resources>
<format id="r1" frameDuration="1/{FPS}s" width="1920" height="1080"/>
<asset id="r2" name="{src_name}" start="0s" duration="2000/{FPS}s" hasVideo="1" hasAudio="1" format="r1">
<media-rep kind="original-media" src="file:///tmp/{src_name}"/></asset></resources>
<library><event name="e"><project name="p"><sequence format="r1" duration="{round(off * FPS)}/{FPS}s">
<spine>{''.join(clips)}</spine></sequence></project></event></library></fcpxml>""")
    return dst


def write_xmeml(dst: Path, src_name: str, ranges: list[tuple[float, float]]) -> Path:
    items, pos = [], 0
    for i, (a, b) in enumerate(ranges):
        fa, fb = round(a * FPS), round(b * FPS)
        items.append(f"<clipitem id=\"c{i}\"><name>{src_name}</name><start>{pos}</start><end>{pos + fb - fa}</end>"
                     f"<in>{fa}</in><out>{fb}</out><file id=\"f1\"><name>{src_name}</name>"
                     f"<pathurl>file://localhost/tmp/{src_name}</pathurl></file></clipitem>")
        pos += fb - fa
    dst.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<xmeml version="4"><sequence><name>p</name><duration>{pos}</duration>
<rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
<media><video><track>{''.join(items)}</track></video></media></sequence></xmeml>""")
    return dst


def write_edl(dst: Path, src_name: str, ranges: list[tuple[float, float]], hour: int = 1) -> Path:
    """CMX3600；记录端从 01:00:00:00 起（NLE 惯例），校验器应按差值算总长。"""
    lines, pos, base = ["TITLE: p", "FCM: NON-DROP FRAME", ""], 0.0, hour * 3600
    for i, (a, b) in enumerate(ranges, 1):
        d = round((b - a) * FPS) / FPS
        lines.append(f"{i:03d}  AX       V     C        {tc(a)} {tc(b)} {tc(base + pos)} {tc(base + pos + d)}")
        lines.append(f"* FROM CLIP NAME: {src_name}")
        pos += d
    dst.write_text("\n".join(lines) + "\n")
    return dst


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def talk() -> dict:
    return truth_of("talk_zh")


@pytest.fixture(scope="module")
def lecture() -> dict:
    return truth_of("lecture_en")


@pytest.fixture(scope="module")
def work(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("verify")


@pytest.fixture(scope="module")
def talk_ideal(work, talk) -> Path:
    return render(MEDIA / "talk_zh.mp4", work / "talk_ideal.mp4", ideal_ranges(talk))


@pytest.fixture(scope="module")
def lecture_ideal(work, lecture) -> Path:
    return render(MEDIA / "lecture_en.mp4", work / "lecture_ideal.mp4", ideal_ranges(lecture))


@pytest.fixture(scope="module")
def talk_clean(work, talk) -> Path:
    return render(MEDIA / "talk_zh.mp4", work / "talk_clean.mp4", ideal_ranges(talk, drop_tagged=True))


def run(name: str, output: Path, task: str) -> dict:
    return verify.verify(MEDIA / f"{name}.truth.json", MEDIA / f"{name}.mp4", output, task)


def by_idx(res: dict) -> dict[int, dict]:
    return {s["idx"]: s for s in res["segments"]}


# ---------------------------------------------------------------- ① 源文件原样 → 全 intact 但 long_pauses fail


def test_source_as_output_has_all_speech_but_dead_air():
    res = run("talk_zh", MEDIA / "talk_zh.mp4", "long_pauses")
    assert not res["pass"]
    assert all(s["status"] == "intact" and not s["clipped"] for s in res["segments"])
    assert all(s["score"] > 0.99 for s in res["segments"])
    assert len(res["dead_air"]) >= 3  # 1.9s / 2.6s / 3.4s 三处中间长停顿
    assert not res["checks"]["no_dead_air"] and res["checks"]["all_speech_intact"]


# ---------------------------------------------------------------- ② 理想剪辑 → pass


def test_ideal_cut_passes_long_pauses(talk_ideal):
    res = run("talk_zh", talk_ideal, "long_pauses")
    assert res["pass"], res["reasons"]
    assert all(s["status"] == "intact" for s in res["segments"])
    assert not res["dead_air"] and res["checks"]["av_drift_ok"]
    assert res["durations"]["removed"] > 5


def test_ideal_cut_passes_noisy_silence(lecture_ideal):
    res = run("lecture_en", lecture_ideal, "noisy_silence")
    assert res["pass"], res["reasons"]
    assert all(s["status"] == "intact" for s in res["segments"])
    assert res["edge_gaps"]["head"] <= 1.0 and res["edge_gaps"]["tail"] <= 1.0


def test_noisy_silence_requires_trimmed_edges(lecture, work):
    """只剪中间、保留 1.5s 开头静音：中间 OK，但 noisy_silence 要求首尾也去掉。"""
    rs = ideal_ranges(lecture)
    rs[0] = (0.0, rs[0][1])  # 开头整段 1.5s 静音保留
    res = run("lecture_en", render(MEDIA / "lecture_en.mp4", work / "lecture_headroom.mp4", rs), "noisy_silence")
    assert not res["pass"] and not res["checks"]["edges_trimmed"]
    assert res["checks"]["all_speech_intact"] and res["checks"]["no_dead_air"]


# ---------------------------------------------------------------- ③ 剪进语音 → partial、fail


def test_cutting_a_sentence_in_half_is_partial(talk, work):
    victim = talk["_speech"][1]  # 最长的一句（6.1s）
    mid = (victim["start"] + victim["end"]) / 2
    rs = []
    for a, b in ideal_ranges(talk):
        if a <= victim["start"] < b:  # 把这一句后半截（到 victim.end）从保留区间里挖掉
            rs += [(a, mid), (victim["end"], b)]
        else:
            rs.append((a, b))
    res = run("talk_zh", render(MEDIA / "talk_zh.mp4", work / "talk_half.mp4", rs), "long_pauses")
    segs = by_idx(res)
    assert segs[1]["status"] == "partial", segs[1]
    assert verify.PARTIAL_MIN <= segs[1]["score"] < verify.INTACT_MIN
    assert all(segs[i]["status"] == "intact" for i in segs if i != 1)
    assert not res["pass"] and not res["checks"]["all_speech_intact"]


def test_clipping_sentence_edges_is_flagged(talk, work):
    """削掉一句话末尾 0.4s：峰值分数仍可能 ≥0.8，但 coverage 应暴露出被削。"""
    victim = talk["_speech"][4]
    rs = []
    for a, b in ideal_ranges(talk):
        if a <= victim["start"] < b:
            rs += [(a, victim["end"] - 0.4), (victim["end"], b)] if victim["end"] < b else [(a, victim["end"] - 0.4)]
        else:
            rs.append((a, b))
    res = run("talk_zh", render(MEDIA / "talk_zh.mp4", work / "talk_clip.mp4", rs), "long_pauses")
    seg = by_idx(res)[4]
    assert seg["status"] != "absent"
    assert seg["status"] == "partial" or seg["clipped"], seg
    assert not res["pass"]


# ---------------------------------------------------------------- ④ retake / filler


def test_retake_filler_ideal_passes(talk_clean):
    res = run("talk_zh", talk_clean, "retake_filler")
    assert res["pass"], res["reasons"]
    segs = by_idx(res)
    tagged = [i for i, s in segs.items() if s["tag"]]
    assert len(tagged) == 3  # 嗯 / 说错的半句 / “不对，我重新说一遍”
    assert all(segs[i]["status"] == "absent" for i in tagged)
    assert all(s["status"] == "intact" for s in segs.values() if not s["tag"])


def test_retake_filler_fails_when_nothing_removed():
    res = run("talk_zh", MEDIA / "talk_zh.mp4", "retake_filler")
    assert not res["pass"] and not res["checks"]["tagged_removed"]
    assert res["checks"]["kept_intact"]


def test_retake_filler_fails_when_good_take_lost(talk, work):
    """把重说后的正确版本剪掉、却留着说错的版本。"""
    rs = [r for r in ideal_ranges(talk) if not (r[0] <= talk["_speech"][6]["start"] < r[1])]
    res = run("talk_zh", render(MEDIA / "talk_zh.mp4", work / "talk_lost.mp4", rs), "retake_filler")
    assert not res["pass"]
    assert by_idx(res)[6]["status"] == "absent" and not res["checks"]["kept_intact"]


def test_english_retake_scores_do_not_cross_match(lecture, work):
    """英文素材里被放弃的 retake 和最终版内容相近，但不能互相误判为存活。"""
    out = render(MEDIA / "lecture_en.mp4", work / "lecture_clean.mp4", ideal_ranges(lecture, drop_tagged=True))
    res = run("lecture_en", out, "retake_filler")
    assert res["pass"], res["reasons"]


# ---------------------------------------------------------------- 基础检查


def test_missing_output_is_no_output(work):
    res = run("talk_zh", work / "nope.mp4", "long_pauses")
    assert not res["pass"] and res["checks"]["exists"] is False
    assert res["reasons"][0].startswith("no_output")


def test_unreadable_output_fails(work):
    bad = work / "garbage.mp4"
    bad.write_bytes(b"not a video" * 100)
    res = run("talk_zh", bad, "long_pauses")
    assert not res["pass"] and res["checks"]["probe_ok"] is False


def test_audio_only_output_fails(work, talk_ideal):
    wav = work / "audio_only.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(talk_ideal), "-vn", "-c:a", "aac", str(wav)], check=True)
    res = run("talk_zh", wav, "long_pauses")
    assert not res["pass"] and res["checks"]["has_video"] is False


def test_av_drift_detected(work, talk_ideal):
    """音频比视频长 1s（视频流被截短）：av_drift 应超标。"""
    drift = work / "drift.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(talk_ideal), "-i", str(talk_ideal), "-map", "0:v",
                    "-map", "1:a", "-af", "apad=pad_dur=1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(drift)], check=True)
    res = run("talk_zh", drift, "long_pauses")
    assert res["checks"]["av_drift"] > verify.AV_DRIFT_MAX and not res["checks"]["av_drift_ok"]
    assert not res["pass"]


# ---------------------------------------------------------------- export_nle


@pytest.mark.parametrize("writer,ext,fmt", [(write_fcpxml, "fcpxml", "fcpxml"), (write_xmeml, "xml", "xmeml"),
                                            (write_edl, "edl", "edl")])
def test_export_ideal_timeline_passes(writer, ext, fmt, talk, work):
    res = run("talk_zh", writer(work / f"ideal.{ext}", "talk_zh.mp4", ideal_ranges(talk)), "export_nle")
    assert res["pass"], res["reasons"]
    assert res["format"] == fmt and res["events"] >= 4
    lo, hi = res["duration_bounds"]
    assert lo <= res["timeline_duration"] <= hi
    assert all(c["covered"] >= 0.9 for c in res["speech_cover"])


def test_export_fcpxmld_bundle(talk, work):
    bundle = work / "p.fcpxmld"
    bundle.mkdir()
    write_fcpxml(bundle / "Info.fcpxml", "talk_zh.mp4", ideal_ranges(talk))
    assert run("talk_zh", bundle, "export_nle")["pass"]


@pytest.mark.parametrize("writer,ext", [(write_fcpxml, "fcpxml"), (write_xmeml, "xml"), (write_edl, "edl")])
def test_export_uncut_timeline_fails(writer, ext, talk, work):
    res = run("talk_zh", writer(work / f"full.{ext}", "talk_zh.mp4", [(0.0, talk["duration"])]), "export_nle")
    assert not res["pass"] and not res["checks"]["duration_in_range"]


def test_export_wrong_source_name_fails(talk, work):
    res = run("talk_zh", write_fcpxml(work / "wrong.fcpxml", "other.mov", ideal_ranges(talk)), "export_nle")
    assert not res["pass"] and not res["checks"]["source_ref"]


def test_export_timeline_that_drops_speech_fails(talk, work):
    """时长落在合法区间内，但把一句该保留的话整个丢了。"""
    rs = [r for r in ideal_ranges(talk) if not (r[0] <= talk["_speech"][0]["start"] < r[1])]
    # 补回一段静音把总长撑进范围：这样只有 speech_covered 能抓到问题
    rs.append((talk["_speech"][-1]["end"], talk["duration"]))
    res = run("talk_zh", write_fcpxml(work / "dropped.fcpxml", "talk_zh.mp4", rs), "export_nle")
    assert not res["pass"] and not res["checks"]["speech_covered"]


def test_export_broken_xml_fails(work):
    p = work / "broken.fcpxml"
    p.write_text("<fcpxml><resources></fcpxml>")
    res = run("talk_zh", p, "export_nle")
    assert not res["pass"] and res["checks"]["parse_ok"] is False


def test_export_empty_edl_fails(work):
    p = work / "empty.edl"
    p.write_text("TITLE: nothing\nFCM: NON-DROP FRAME\n")
    res = run("talk_zh", p, "export_nle")
    assert not res["pass"] and res["checks"]["parse_ok"] is False


def test_export_missing_file_is_no_output(work):
    res = run("talk_zh", work / "none.fcpxml", "export_nle")
    assert not res["pass"] and res["reasons"][0].startswith("no_output")


# ---------------------------------------------------------------- CLI


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "evals" / "verify.py"), *args], capture_output=True, text=True)


def test_cli_exit_codes_and_json(talk_ideal):
    base = ["--truth", str(MEDIA / "talk_zh.truth.json"), "--source", str(MEDIA / "talk_zh.mp4")]
    ok = cli(*base, "--output", str(talk_ideal), "--task", "long_pauses", "--json")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    data = json.loads(ok.stdout)
    assert data["pass"] is True and len(data["segments"]) == 10
    bad = cli(*base, "--output", str(MEDIA / "talk_zh.mp4"), "--task", "long_pauses")
    assert bad.returncode == 1 and "FAIL" in bad.stdout
    usage = cli("--truth", "/nonexistent.json", "--source", "x", "--output", "y", "--task", "long_pauses")
    assert usage.returncode == 2
    assert cli(*base, "--output", "y", "--task", "nonsense").returncode == 2
