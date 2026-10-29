"""evals/verify_genres.py 自测：每个任务至少一个应 pass 的理想产物 + 应 fail 的产物。

产物全部生成在 pytest 的 tmp_path 里。素材在 fixtures/media/（gitignored），缺失时整模块跳过；
T8 的配音用 macOS `say` 合成，没有 `say` 时只跳过那两个测试。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import _media as M  # noqa: E402
import verify_genres as G  # noqa: E402

MEDIA = M.MEDIA
NEEDED = ["game_commentary.mp4", "stream_vod.mp4", "montage/clip1.mp4", "montage/music_120bpm.wav",
          "knowledge/script.md", "ytpmv/source_clip.mp4", "ytpmv/music_120bpm.wav"]
if not all((MEDIA / n).exists() for n in NEEDED):
    pytest.skip("第二批 fixtures 缺失（game_commentary / stream_vod / montage / knowledge / ytpmv）", allow_module_level=True)


def truth(name: str) -> dict:
    return json.loads((MEDIA / f"{name}.truth.json").read_text())


def run(task: str, name: str, source: Path, output: Path) -> dict:
    return G.verify(MEDIA / f"{name}.truth.json", source, output, task)


@pytest.fixture(scope="module")
def work(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("genres")


# ================================================================ T5 game_commentary


def game_ranges(boring_keep: float, drop_action: bool = False) -> list[tuple[float, float]]:
    out = []
    for s in truth("game_commentary")["segments"]:
        if s["kind"] == "gap" and s.get("tag") == "boring":
            out.append((s["start"], s["start"] + boring_keep))
        elif s["kind"] == "gap" and s.get("tag") == "action" and drop_action:
            continue
        else:
            out.append((s["start"], s["end"]))
    return M.merge(out)


@pytest.fixture(scope="module")
def game_ideal(work) -> Path:
    return M.render_ranges(MEDIA / "game_commentary.mp4", work / "game_ideal.mp4", game_ranges(0.4))


def test_game_ideal_passes(game_ideal):
    res = run("game_commentary", "game_commentary", MEDIA / "game_commentary.mp4", game_ideal)
    assert res["pass"], res["reasons"]
    assert all(r["status"] == "intact" for r in res["speech"])
    assert all(e["present"] for e in res["events"]) and len(res["events"]) == 3
    assert all(g["kept"] <= 1.0 for g in res["gaps"] if g["tag"] == "boring")
    assert res["durations"]["removed"] > 8
    assert res["loudness"]["lufs"] is not None


def test_game_boring_residual_tolerates_one_frame_but_not_more(work):
    src = MEDIA / "game_commentary.mp4"
    ok = M.render_ranges(src, work / "game_keep1_02.mp4", game_ranges(1.02))
    assert run("game_commentary", "game_commentary", src, ok)["pass"]
    bad = M.render_ranges(src, work / "game_keep1_3.mp4", game_ranges(1.3))
    res = run("game_commentary", "game_commentary", src, bad)
    assert not res["pass"] and not res["checks"]["boring_residual_ok"]
    assert all(1.2 < g["kept"] < 1.4 for g in res["gaps"] if g["tag"] == "boring")


def test_game_uncut_fails_on_boring_residual():
    src = MEDIA / "game_commentary.mp4"
    res = run("game_commentary", "game_commentary", src, src)
    assert not res["pass"] and not res["checks"]["boring_residual_ok"]
    assert res["checks"]["all_speech_intact"] and res["checks"]["all_events_present"]
    assert all(g["kept"] > 5 for g in res["gaps"] if g["tag"] == "boring")


def test_game_cutting_the_action_fails_on_events(work):
    """把没说话、但有爆炸的 action 间隔当成无聊段剪掉。"""
    out = M.render_ranges(MEDIA / "game_commentary.mp4", work / "game_noaction.mp4", game_ranges(0.4, drop_action=True))
    res = run("game_commentary", "game_commentary", MEDIA / "game_commentary.mp4", out)
    assert not res["pass"] and not res["checks"]["all_events_present"]
    assert res["checks"]["boring_residual_ok"]


def test_game_cutting_into_speech_fails(work):
    rs = [(a, b - 0.8) if a < 6 < b else (a, b) for a, b in game_ranges(0.4)]
    out = M.render_ranges(MEDIA / "game_commentary.mp4", work / "game_trimmed.mp4", rs)
    res = run("game_commentary", "game_commentary", MEDIA / "game_commentary.mp4", out)
    assert not res["pass"] and not res["checks"]["all_speech_intact"]


# ================================================================ T6 stream_clips


HL_CLIPS = [(28.0, 18.0), (78.0, 18.0), (126.0, 18.0)]  # 各含 highlight 前 5s 铺垫


@pytest.fixture(scope="module")
def clips_ideal(work) -> Path:
    d = work / "clips_ideal"
    d.mkdir()
    for i, (s, dur) in enumerate(HL_CLIPS, 1):
        M.vertical_clip(MEDIA / "stream_vod.mp4", d / f"clip_{i}.mp4", s, dur)
    M.write_srt(d / "clip_1.srt", [(0, 5, "我去！这也能秒？！")])
    return d


def test_clips_ideal_passes(clips_ideal):
    res = run("stream_clips", "stream_vod", MEDIA / "stream_vod.mp4", clips_ideal)
    assert res["pass"], res["reasons"]
    hl = [tuple(c["covers_highlights"]) for c in res["clips"]]
    assert hl == [(0,), (1,), (2,)]
    assert all(abs(c["ratio"] - 9 / 16) < 0.01 and 15 <= c["duration"] <= 30 for c in res["clips"])
    assert res["subtitles_status"] == ["verified", "burned_or_missing", "burned_or_missing"]  # 只记录，不影响 pass
    assert not any(c["overlaps_decoy"] for c in res["clips"])


def test_clips_decoy_instead_of_highlight_fails(clips_ideal, work):
    d = work / "clips_decoy"
    shutil.copytree(clips_ideal, d)
    M.vertical_clip(MEDIA / "stream_vod.mp4", d / "clip_3.mp4", 56.0, 18.0)  # 抽奖刷屏段 59.5–70，不含任何 highlight
    res = run("stream_clips", "stream_vod", MEDIA / "stream_vod.mp4", d)
    assert not res["pass"] and not res["checks"]["distinct_highlights"]
    assert res["clips"][2]["overlaps_decoy"] and res["clips"][2]["covers_highlights"] == []


def test_clips_two_clips_on_same_highlight_fails(clips_ideal, work):
    d = work / "clips_dup"
    shutil.copytree(clips_ideal, d)
    M.vertical_clip(MEDIA / "stream_vod.mp4", d / "clip_2.mp4", 27.0, 18.0)  # 又切了第 1 个 highlight
    res = run("stream_clips", "stream_vod", MEDIA / "stream_vod.mp4", d)
    assert not res["pass"] and not res["checks"]["distinct_highlights"]
    assert res["checks"]["durations_ok"] and res["checks"]["vertical_ok"]


def test_clips_landscape_and_wrong_count_fail(work):
    d = work / "clips_landscape"
    d.mkdir()
    for i, (s, dur) in enumerate(HL_CLIPS[:2], 1):
        M.vertical_clip(MEDIA / "stream_vod.mp4", d / f"clip_{i}.mp4", s, dur, vertical=False)
    res = run("stream_clips", "stream_vod", MEDIA / "stream_vod.mp4", d)
    assert not res["pass"]
    assert not res["checks"]["exactly_three_files"] and not res["checks"]["vertical_ok"]


def test_clips_too_short_and_extra_file_fail(clips_ideal, work):
    d = work / "clips_short"
    shutil.copytree(clips_ideal, d)
    M.vertical_clip(MEDIA / "stream_vod.mp4", d / "clip_1.mp4", 31.0, 8.0)
    M.vertical_clip(MEDIA / "stream_vod.mp4", d / "clip_4.mp4", 100.0, 16.0)
    res = run("stream_clips", "stream_vod", MEDIA / "stream_vod.mp4", d)
    assert not res["pass"] and not res["checks"]["durations_ok"] and not res["checks"]["exactly_three_files"]
    assert res["extras"] == ["clip_4.mp4"]


def test_clips_output_not_a_directory_is_no_output(work):
    res = run("stream_clips", "stream_vod", MEDIA / "stream_vod.mp4", work / "nonexistent_dir")
    assert not res["pass"] and res["reasons"][0].startswith("no_output")


# ================================================================ T7 montage


PLAN_ON_BEAT = [(1, 2.0), (2, 2.5), (3, 2.5), (4, 2.5), (5, 2.5), (6, 3.0)]  # 切点 2.0/4.5/7.0/9.5/12.0
PLAN_OFF_BEAT = [(i, 1.37 + 0.11 * i) for i in range(1, 7)] + [(1, 1.0)]


@pytest.fixture(scope="module")
def montage_ideal(work) -> Path:
    return M.montage(MEDIA / "montage", work / "montage_ideal.mp4", PLAN_ON_BEAT, MEDIA / "montage" / "music_120bpm.wav")


def test_montage_ideal_passes(montage_ideal):
    res = run("montage", "montage", MEDIA / "montage", montage_ideal)
    assert res["pass"], res["reasons"]
    assert res["bgm"]["score"] >= 0.9 and abs(res["bgm"]["offset"]) < 0.05
    assert len(res["cuts"]) >= 5 and res["cut_aligned_frac"] == 1.0


def test_montage_bgm_starting_mid_song_still_passes(work):
    """BGM 从第 2s 起（整拍）：偏移应被求出，切点相对 BGM 时间轴仍在拍上。"""
    out = M.montage(MEDIA / "montage", work / "montage_off2.mp4", PLAN_ON_BEAT, MEDIA / "montage" / "music_120bpm.wav", 2.0)
    res = run("montage", "montage", MEDIA / "montage", out)
    assert res["pass"], res["reasons"]
    # BGM 是 8s 循环：偏移只能确定到循环周期（拍长的整数倍）；按拍长取模比较
    assert abs((res["bgm"]["offset"] - 2.0 + 0.25) % 0.5 - 0.25) < 0.05


def test_montage_cuts_off_the_beat_fail(work):
    out = M.montage(MEDIA / "montage", work / "montage_offbeat.mp4", PLAN_OFF_BEAT, MEDIA / "montage" / "music_120bpm.wav")
    res = run("montage", "montage", MEDIA / "montage", out)
    assert not res["pass"] and not res["checks"]["cuts_on_beat"] and res["checks"]["bgm_present"]


def test_montage_bgm_shifted_by_a_fraction_of_beat_fails(work):
    """切点在产物时间轴上是整拍，但 BGM 被往后挪了 0.2s：相对 BGM 就不在拍上。"""
    out = M.montage(MEDIA / "montage", work / "montage_shift.mp4", PLAN_ON_BEAT, MEDIA / "montage" / "music_120bpm.wav", 0.2)
    res = run("montage", "montage", MEDIA / "montage", out)
    assert not res["pass"] and not res["checks"]["cuts_on_beat"]


def test_montage_without_bgm_fails(work):
    out = M.montage(MEDIA / "montage", work / "montage_nobgm.mp4", PLAN_ON_BEAT, None)
    res = run("montage", "montage", MEDIA / "montage", out)
    assert not res["pass"] and not res["checks"]["bgm_present"]


def test_montage_too_short_fails(work):
    out = M.montage(MEDIA / "montage", work / "montage_short.mp4", PLAN_ON_BEAT[:3], MEDIA / "montage" / "music_120bpm.wav")
    res = run("montage", "montage", MEDIA / "montage", out)
    assert not res["pass"] and not res["checks"]["duration_ok"]


# ================================================================ T8 knowledge


have_say = shutil.which("say") is not None


@pytest.fixture(scope="module")
def narration(work) -> Path:
    if not have_say:
        pytest.skip("没有 macOS say")
    script = work / "narr.txt"
    script.write_text("\n".join(truth("knowledge")["sentences"]))
    aiff = work / "narr.aiff"
    subprocess.run(["say", "-v", "Tingting", "-o", str(aiff), "-f", str(script)], check=True)
    return aiff


def knowledge_video(work: Path, name: str, narr: Path | None, images: list[str], size: str = "1280:720",
                    dur: float | None = None, srt: bool = True) -> Path:
    t = truth("knowledge")
    d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(narr)],
                             capture_output=True, text=True).stdout) if narr else 30.0
    d = dur or d
    per = d / len(images)
    inputs, fc = [], []
    for i, img in enumerate(images):
        inputs += ["-loop", "1", "-t", f"{per:.3f}", "-i", str(MEDIA / "knowledge" / img)]
        fc.append(f"[{i}:v]scale={size},setsar=1,fps=30,format=yuv420p[v{i}]")
    fc.append("".join(f"[v{i}]" for i in range(len(images))) + f"concat=n={len(images)}:v=1:a=0[v]")
    out = work / f"{name}.mp4"
    if narr:
        M.ff(*inputs, "-i", str(narr), "-t", f"{d:.3f}", "-filter_complex", ";".join(fc), "-map", "[v]", "-map", f"{len(images)}:a",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out))
    else:  # 静音
        M.ff(*inputs, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", f"{d:.3f}", "-filter_complex", ";".join(fc),
             "-map", "[v]", "-map", f"{len(images)}:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out))
    if srt:
        n = len(t["sentences"])
        M.write_srt(out.with_suffix(".srt"), [(d * i / n, d * (i + 1) / n, s) for i, s in enumerate(t["sentences"])])
    return out


IMGS = ["img1.png", "img2.png", "img3.png"]


def test_knowledge_ideal_passes(work, narration):
    out = knowledge_video(work, "knowledge_ideal", narration, IMGS)
    res = run("knowledge", "knowledge", MEDIA / "knowledge", out)
    assert res["pass"], res["reasons"]
    assert res["images_used"] == 3 and all(i["seconds_shown"] >= 5 for i in res["images"])
    assert res["subtitles"]["status"] == "verified" and res["subtitles"]["script_char_overlap"] > 0.95
    assert res["nonsilent_frac"] >= 0.5


def test_knowledge_without_subtitles_still_passes_but_is_recorded(work, narration):
    out = knowledge_video(work, "knowledge_nosub", narration, IMGS, srt=False)
    res = run("knowledge", "knowledge", MEDIA / "knowledge", out)
    assert res["pass"], res["reasons"]
    assert res["subtitles"]["status"] == "burned_or_missing"


def test_knowledge_portrait_silent_and_single_image_fail(work, narration):
    portrait = knowledge_video(work, "knowledge_portrait", narration, IMGS, size="720:1280")
    r1 = run("knowledge", "knowledge", MEDIA / "knowledge", portrait)
    assert not r1["pass"] and not r1["checks"]["landscape"]
    silent = knowledge_video(work, "knowledge_silent", None, IMGS, dur=33.0)
    r2 = run("knowledge", "knowledge", MEDIA / "knowledge", silent)
    assert not r2["pass"] and not r2["checks"]["has_speech"]
    one = knowledge_video(work, "knowledge_one_img", narration, IMGS[:1])
    r3 = run("knowledge", "knowledge", MEDIA / "knowledge", one)
    assert not r3["pass"] and r3["images_used"] == 1 and not r3["checks"]["images_ok"]


def test_knowledge_too_short_fails(work, narration):
    out = knowledge_video(work, "knowledge_short", narration, IMGS, dur=12.0)
    res = run("knowledge", "knowledge", MEDIA / "knowledge", out)
    assert not res["pass"] and not res["checks"]["duration_ok"]


def test_knowledge_subtitle_text_mismatch_is_flagged(work, narration):
    out = knowledge_video(work, "knowledge_badsub", narration, IMGS)
    M.write_srt(out.with_suffix(".srt"), [(0, 5, "完全无关的字幕 hello world")])
    res = run("knowledge", "knowledge", MEDIA / "knowledge", out)
    assert res["subtitles"]["status"] == "present_text_mismatch"


# ================================================================ T9 ytpmv


def ytpmv_audio(schedule: list[tuple[int, float]], bgm_gain: float = 0.5, offset: float = 0.0, sr: int = 48000) -> np.ndarray:
    """BGM 为底，把 truth 音节按 schedule=[(音节序号, 起点秒)] 放进去。"""
    t = truth("ytpmv")
    bgm = M.pcm(MEDIA / "ytpmv" / "music_120bpm.wav", sr) * bgm_gain
    src = M.pcm(MEDIA / "ytpmv" / "source_clip.mp4", sr)
    out = bgm.copy() if bgm_gain else np.zeros_like(bgm)
    for i, at in schedule:
        s = t["syllables"][i]
        seg = src[int(s["start"] * sr): int(s["end"] * sr)].copy()
        k = int((at + offset) * sr)
        out[k: k + len(seg)] += seg[: len(out) - k]
    return out


BEAT_SCHEDULE = [(i % 12, 2.0 + 0.5 * i) for i in range(16)]  # 每个音节都踩在整拍上，共 16 次


def test_ytpmv_ideal_passes(work):
    out = M.mux_audio(ytpmv_audio(BEAT_SCHEDULE), work / "ytpmv_ideal.mp4")
    res = run("ytpmv", "ytpmv", MEDIA / "ytpmv", out)
    assert res["pass"], res["reasons"]
    assert res["occurrence_count"] >= 12 and res["aligned_frac"] >= 0.9 and res["distinct_syllables"] >= 6
    assert res["checks"]["has_repeats"]


def test_ytpmv_bgm_only_fails_on_syllables(work):
    out = M.mux_audio(ytpmv_audio([]), work / "ytpmv_bgm_only.mp4")
    res = run("ytpmv", "ytpmv", MEDIA / "ytpmv", out)
    assert not res["pass"] and res["checks"]["bgm_present"] and not res["checks"]["enough_occurrences"]


def test_ytpmv_syllables_off_the_beat_fail(work):
    out = M.mux_audio(ytpmv_audio(BEAT_SCHEDULE, offset=0.12), work / "ytpmv_offbeat.mp4")
    res = run("ytpmv", "ytpmv", MEDIA / "ytpmv", out)
    assert not res["pass"] and res["checks"]["enough_occurrences"] and not res["checks"]["on_beat"]


def test_ytpmv_without_bgm_fails(work):
    out = M.mux_audio(ytpmv_audio(BEAT_SCHEDULE, bgm_gain=0.0), work / "ytpmv_nobgm.mp4")
    res = run("ytpmv", "ytpmv", MEDIA / "ytpmv", out)
    assert not res["pass"] and not res["checks"]["bgm_present"]


def test_ytpmv_original_clip_only_fails(work):
    """只是把原片放了一遍，没有重复、没有 BGM。"""
    res = run("ytpmv", "ytpmv", MEDIA / "ytpmv", MEDIA / "ytpmv" / "source_clip.mp4")
    assert not res["pass"] and not res["checks"]["bgm_present"]
    assert res["occurrence_count"] == 12 and not res["checks"]["has_repeats"]  # 每个音节恰好一次：满足 ≥6 但没有重复


# ================================================================ 基础 / CLI


@pytest.mark.parametrize("task,name,source", [("game_commentary", "game_commentary", "game_commentary.mp4"),
                                              ("montage", "montage", "montage"), ("knowledge", "knowledge", "knowledge"),
                                              ("ytpmv", "ytpmv", "ytpmv")])
def test_missing_output_is_no_output(task, name, source, work):
    res = run(task, name, MEDIA / source, work / "does_not_exist.mp4")
    assert not res["pass"] and res["checks"]["exists"] is False and res["reasons"][0].startswith("no_output")


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(M.ROOT / "evals" / "verify_genres.py"), *args], capture_output=True, text=True)


def test_cli_exit_codes_and_json(game_ideal):
    base = ["--truth", str(MEDIA / "game_commentary.truth.json"), "--source", str(MEDIA / "game_commentary.mp4"),
            "--task", "game_commentary"]
    ok = cli(*base, "--output", str(game_ideal), "--json")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert json.loads(ok.stdout)["pass"] is True
    bad = cli(*base, "--output", str(MEDIA / "game_commentary.mp4"))
    assert bad.returncode == 1 and "FAIL" in bad.stdout
    assert cli("--truth", "/nonexistent.json", "--source", "x", "--output", "y", "--task", "ytpmv").returncode == 2
    assert cli(*base[:4], "--output", "y", "--task", "nonsense").returncode == 2
