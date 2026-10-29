"""发布前体检：死寂、开头、字幕、响度、画幅/时长、切片首尾落在句中。每一项都要有时间点和可执行的修法，并且声明"不是爆款保证"。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from autocut.check import DISCLAIMER, CheckReport, check
from autocut.plan import Captions, Segment
from autocut.render import render

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_render import talk_plan  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))


def ids(rep: CheckReport, item_id: str) -> list:
    return [i for i in rep.items if i.id == item_id]


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def ff(*args: str) -> None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------------ 报告格式与诚实


def test_report_schema_json_and_honesty() -> None:
    rep = check(MEDIA / "talk_zh.mp4", asr=False)
    d = rep.to_dict()
    assert set(d) >= {"target", "platform", "duration", "metrics", "items", "summary"}
    json.loads(json.dumps(d, ensure_ascii=False))  # 能存档
    assert DISCLAIMER in d["summary"] and "不是爆款保证" in d["summary"] and d["disclaimer"] == DISCLAIMER
    assert d["items"], "talk_zh 至少有长停顿"
    for it in d["items"]:
        assert set(it) == {"id", "severity", "at", "end", "message", "fix"}
        assert it["severity"] in ("risk", "warn", "info") and it["message"] and it["fix"], it
    text = json.dumps(d, ensure_ascii=False)
    for banned in ("爆款分", "病毒", "viral score", "病毒指数"):
        assert banned not in text
    assert "不是爆款保证" in rep.to_text()
    saved = rep.save(Path(tempfile.mkdtemp()) / "sub" / "check.json")  # 发布前存档，事后与留存曲线对齐
    back = json.loads(saved.read_text(encoding="utf-8"))
    assert back["items"] == d["items"] and back["schema"] == 1 and back["checked_at"]


def test_every_time_item_has_at_and_actionable_fix() -> None:
    rep = check(MEDIA / "stream_vod.mp4", platform="douyin", asr=False)
    for it in rep.items:
        assert it.fix.strip() and len(it.fix) >= 6
        assert it.at is not None and it.end is not None and 0 <= it.at <= it.end <= rep.duration + 0.05, it  # 全片性的问题标成整段
        if it.id in ("loudness_low", "aspect", "duration", "resolution"):
            assert (it.at, it.end) == (0.0, rep.duration)


# ------------------------------------------------------------------ 死寂


def test_talk_zh_original_reports_every_long_pause(truth) -> None:
    t = truth("talk_zh")
    gaps = [(s["start"], s["end"]) for s in t["segments"] if s["kind"] == "gap" and s["end"] - s["start"] >= 1.5]
    assert len(gaps) >= 3
    rep = check(MEDIA / "talk_zh.mp4", asr=False)
    dead = ids(rep, "dead_air")
    for g0, g1 in gaps:
        hit = [d for d in dead if overlap(d.at, d.end, g0, g1) >= 0.7 * (g1 - g0) - 0.3]
        assert hit, f"truth 长停顿 {g0}-{g1}s 没有被报告：{[(d.at, d.end) for d in dead]}"
        assert "剪" in hit[0].fix or "缩" in hit[0].fix
    assert rep.metrics["dead_air_total_s"] >= sum(b - a for a, b in gaps) * 0.7
    # 画面一直在动（逐帧动画）→ 提醒而不是"无声无动"的风险
    assert all(d.severity == "warn" for d in dead)


def test_rendered_output_has_no_dead_air(tmp_path: Path) -> None:
    orig = check(MEDIA / "talk_zh.mp4", asr=False)
    r = render(talk_plan(tmp_path))[0]
    rep = check(r.path, asr=False)
    assert not ids(rep, "dead_air")
    assert rep.metrics["dead_air_total_s"] < orig.metrics["dead_air_total_s"]
    assert rep.metrics["captions"] == "unknown"


def test_dead_air_is_risk_when_silent_and_static(tmp_path: Path) -> None:
    """没声音、画面也不动 → 风险（S88）。造一条：静止画面 + 3 秒静音夹在有声内容中间。"""
    out = tmp_path / "static.mp4"
    ff("-f", "lavfi", "-i", "color=c=gray:s=640x360:r=30:d=8", "-f", "lavfi", "-i", "sine=frequency=300:duration=8", "-filter_complex",
       "[1:a]volume=enable='between(t,2,5)':volume=0,volume=0.3[a]", "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out))
    rep = check(out, platform="youtube", asr=False)
    d = ids(rep, "dead_air")
    assert len(d) == 1 and d[0].severity == "risk" and 1.7 < d[0].at < 2.4 and 4.6 < d[0].end < 5.4, [(x.at, x.end, x.severity) for x in d]
    assert "剪掉" in d[0].fix


# ------------------------------------------------------------------ 开头


def _with_lead_in(tmp_path: Path, moving: bool, lead: float) -> Path:
    """前 lead 秒无声（画面静止或在动），然后是 talk_zh 的前 10 秒。"""
    out = tmp_path / f"lead_{'move' if moving else 'static'}.mp4"
    v0 = f"testsrc2=s=640x360:r=30:d={lead}" if moving else f"color=c=gray:s=640x360:r=30:d={lead}"
    ff("-f", "lavfi", "-i", v0, "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={lead}", "-t", "10", "-i", str(MEDIA / "talk_zh.mp4"), "-filter_complex",
       "[0:v]format=yuv420p,setsar=1[v0];[1:a]aformat=sample_rates=48000:channel_layouts=mono[a0];[2:v]setsar=1[v1];[2:a]aformat=sample_rates=48000:channel_layouts=mono[a1];"
       "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-c:a", "aac", str(out))
    return out


def test_empty_opening_is_a_risk(tmp_path: Path) -> None:
    rep = check(_with_lead_in(tmp_path, moving=False, lead=5.0), platform="douyin", asr=False)
    it = ids(rep, "hook_empty")
    assert len(it) == 1 and it[0].severity == "risk" and it[0].at == 0.0
    assert rep.metrics["opening_empty"] is True and 4.5 < rep.metrics["first_voice_s"] < 5.6
    assert "剪掉" in it[0].fix or "第 0 秒" in it[0].fix


def test_late_first_sentence_says_when_and_what_to_do(tmp_path: Path) -> None:
    rep = check(_with_lead_in(tmp_path, moving=True, lead=5.0), platform="douyin", asr=False)
    it = ids(rep, "hook_late")
    assert len(it) == 1 and it[0].severity == "warn"
    assert 4.5 < it[0].at + (it[0].end - it[0].at) < 5.6 and "挪到开头" in it[0].fix and "剪掉前" in it[0].fix
    rep = check(_with_lead_in(tmp_path, moving=True, lead=8.0), platform="douyin", asr=False)
    assert ids(rep, "hook_late")[0].severity == "risk"
    ok = check(MEDIA / "talk_zh.mp4", asr=False)
    assert not ids(ok, "hook_late") and not ids(ok, "hook_empty")


# ------------------------------------------------------------------ 画幅、时长、平台


def test_landscape_150s_on_douyin_reports_aspect_and_duration() -> None:
    rep = check(MEDIA / "stream_vod.mp4", platform="douyin", asr=False)
    assert rep.platform == "douyin" and rep.duration == pytest.approx(150.0, abs=0.1)
    asp, dur = ids(rep, "aspect"), ids(rep, "duration")
    assert asp and asp[0].severity == "risk" and "9:16" in asp[0].message and "reframe" in asp[0].fix
    assert dur and dur[0].severity in ("warn", "risk") and "150" in dur[0].message
    assert ids(rep, "resolution")  # 640x360 偏低
    ok = check(MEDIA / "stream_vod.mp4", platform="youtube", asr=False)
    assert not ids(ok, "aspect") and not ids(ok, "duration")


def test_platform_auto_guess_from_aspect(tmp_path: Path) -> None:
    assert check(MEDIA / "talk_zh.mp4", asr=False).platform == "bilibili"
    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=3.2, end=9.2)], width=720, height=1280)
    from autocut.plan import Reframe

    plan.outputs[0].reframe = Reframe(mode="center")
    r = render(plan)[0]
    rep = check(r.path, asr=False)
    assert rep.platform == "douyin" and rep.metrics["platform_guessed"] is True
    assert not ids(rep, "aspect") and rep.metrics["size"] == "720x1280"
    assert check(r.path, platform="bilibili", asr=False).platform == "bilibili"
    # 竖屏发到横屏平台：提醒
    assert ids(check(r.path, platform="youtube", asr=False), "aspect")


def test_unknown_platform_and_missing_file() -> None:
    with pytest.raises(ValueError, match="douyin"):
        check(MEDIA / "talk_zh.mp4", platform="weibo")
    with pytest.raises(FileNotFoundError, match="nope.mp4"):
        check(MEDIA / "nope.mp4")


# ------------------------------------------------------------------ 响度


def test_loudness_low_high_and_true_peak(tmp_path: Path) -> None:
    quiet = tmp_path / "quiet.mp4"
    ff("-i", str(MEDIA / "talk_zh.mp4"), "-t", "12", "-af", "volume=-14dB", "-c:v", "copy", str(quiet))
    rep = check(quiet, platform="douyin", asr=False)
    lo = ids(rep, "loudness_low")
    assert lo and lo[0].severity in ("warn", "risk") and "loudnorm" in lo[0].fix
    assert rep.metrics["loudness"]["integrated_lufs"] < -25
    hot = tmp_path / "hot.mp4"
    ff("-i", str(MEDIA / "talk_zh.mp4"), "-t", "12", "-af", "volume=14dB,alimiter=limit=1:level=0", "-c:v", "copy", str(hot))
    rep = check(hot, platform="douyin", asr=False)
    assert ids(rep, "loudness_high") and ids(rep, "true_peak")
    assert rep.metrics["loudness"]["true_peak_dbtp"] > -1.0


def test_loudnormed_vertical_render_passes_loudness(tmp_path: Path) -> None:
    from autocut.plan import Reframe

    plan = talk_plan(tmp_path, [Segment(id="a", source="a", start=3.2, end=9.2)], width=720, height=1280, reframe=Reframe(mode="center"))
    rep = check(render(plan)[0].path, platform="douyin", asr=False)
    assert abs(rep.metrics["loudness"]["integrated_lufs"] + 14.0) < 1.0
    assert not ids(rep, "loudness_low") and not ids(rep, "loudness_high") and not ids(rep, "true_peak")


# ------------------------------------------------------------------ 字幕


def test_captions_burned_sidecar_unknown(tmp_path: Path) -> None:
    segs = [Segment(id="a", source="a", start=0.0, end=2.8108), Segment(id="b", source="a", start=3.1608, end=6.0)]
    plan = talk_plan(tmp_path, segs, captions=Captions(enabled=True, burn=True))
    r = render(plan)[0]
    rep = check(r.path, asr=False)
    assert rep.metrics["captions"] == "burned" and not [i for i in rep.items if i.id.startswith("captions")]
    plan.outputs[0].captions.burn = False
    plan.outputs[0].id = "side"
    r2 = render(plan)[0]
    rep2 = check(r2.path, asr=False)
    assert rep2.metrics["captions"] == "sidecar" and ids(rep2, "captions_sidecar")
    rep3 = check(MEDIA / "talk_zh.mp4", asr=False)
    unk = ids(rep3, "captions_unknown")
    assert rep3.metrics["captions"] == "unknown" and unk and "无法确认是否有烧录字幕" in unk[0].message


# ------------------------------------------------------------------ 切片首尾落在句中


def test_cut_in_the_middle_of_speech_is_flagged_acoustically(tmp_path: Path) -> None:
    # talk_zh 5.34–9.26 是连续的一段人声（detect_speech 实测）；从 6.3 切到 8.3，两头都在说话
    r = render(talk_plan(tmp_path, [Segment(id="a", source="a", start=6.3, end=8.3)]))[0]
    rep = check(r.path, asr=False)
    assert ids(rep, "start_midspeech") and ids(rep, "end_midspeech")
    assert ids(rep, "end_midspeech")[0].at > 1.5
    # 对照：在句子边界上切（前后各留 0.3s 静音）不报
    r2 = render(talk_plan(tmp_path, [Segment(id="a", source="a", start=2.9, end=9.5)]))[0]
    rep2 = check(r2.path, asr=False)
    assert not ids(rep2, "start_midspeech") and not ids(rep2, "end_midspeech")


def test_plan_target_flags_midsentence_edges_from_words(tmp_path: Path) -> None:
    mid = check(talk_plan(tmp_path, [Segment(id="a", source="a", start=5.0, end=7.0)]), asr=False)
    assert mid.mode == "plan"
    s, e = ids(mid, "start_midsentence"), ids(mid, "end_midsentence")
    assert s and e and "上一个句号" in s[0].fix and "说完" in e[0].fix
    good = check(talk_plan(tmp_path, [Segment(id="a", source="a", start=3.1608, end=9.2534)]), asr=False)
    assert not ids(good, "start_midsentence") and not ids(good, "end_midsentence")


# ------------------------------------------------------------------ 按方案估算


def test_plan_estimate_matches_the_rendered_result(tmp_path: Path) -> None:
    keep_all = [Segment(id="a", source="a", start=0.0, end=40.7)]
    before = check(talk_plan(tmp_path, keep_all), asr=False)
    dead = ids(before, "dead_air")
    assert before.mode == "plan" and len(dead) >= 3
    t = json.loads((MEDIA / "talk_zh.truth.json").read_text())
    for s in t["segments"]:
        if s["kind"] == "gap" and s["end"] - s["start"] >= 1.5:
            assert any(overlap(d.at, d.end, s["start"], s["end"]) > 0.5 * (s["end"] - s["start"]) for d in dead), s
    after = check(talk_plan(tmp_path), asr=False)  # 默认方案：长停顿缩到 0.4s
    assert not ids(after, "dead_air") and after.duration == pytest.approx(32.34, abs=0.05)
    assert abs(after.metrics["loudness"]["integrated_lufs"] - check(render(talk_plan(tmp_path))[0].path, asr=False).metrics["loudness"]["integrated_lufs"]) < 1.5
    # 方案没开字幕、说话为主 → 明确说"没有"，而不是"无法确认"
    miss = ids(after, "captions_missing")
    assert miss and after.metrics["captions"] == "none"
    on = check(talk_plan(tmp_path, captions=Captions(enabled=True, burn=True)), asr=False)
    assert on.metrics["captions"] == "burned" and not ids(on, "captions_missing")
