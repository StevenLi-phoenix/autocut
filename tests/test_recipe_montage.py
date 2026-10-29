"""montage 配方：卡点混剪。真值来自 fixtures/media/montage.truth.json（120 BPM，每小节 2s）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pytest

from autocut.beats import BeatResult
from autocut.plan import Plan
from autocut.recipes import montage as M
from autocut.recipes._common import BeatGrid, RecipeError, fit_beat_grid, plan_bars

log = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def clips(media: Path) -> list[Path]:
    ps = [media / "montage" / f"clip{i}.mp4" for i in range(1, 7)]
    if not all(p.exists() for p in ps):
        pytest.skip("缺少 montage 素材：先运行 fixtures/make_fixtures.py")
    return ps


@pytest.fixture(scope="module")
def music(media: Path) -> Path:
    return media / "montage" / "music_120bpm.wav"


@pytest.fixture(scope="module")
def tr(media: Path) -> dict:
    return json.loads((media / "montage.truth.json").read_text())


@pytest.fixture(scope="module")
def plan(clips: list[Path], music: Path, tmp_path_factory: pytest.TempPathFactory) -> Plan:
    return M.build(clips, music=music, out=tmp_path_factory.mktemp("mt") / "m.autocut.json")


# ------------------------------------------------------------------ 集成


@pytest.mark.integration
def test_duration_is_whole_bars_nearest_to_15s(plan: Plan, tr: dict) -> None:
    bar = 60.0 / tr["bpm"] * 4
    o = plan.outputs[0]
    bars = o.duration / bar
    assert bars == pytest.approx(round(bars), abs=1e-9), f"时长 {o.duration}s 不是整小节（每小节 {bar}s）"
    assert abs(o.duration - 15.0) <= bar / 2 + 1e-9, f"{o.duration}s 不是最接近 15s 的整小节"


@pytest.mark.integration
def test_every_cut_lands_on_a_beat_within_1ms(plan: Plan, tr: dict) -> None:
    beats = np.asarray(tr["beats"])
    o = plan.outputs[0]
    cuts = [t0 for _, t0, _ in o.timeline()] + [o.duration]
    for t in cuts:
        assert np.min(np.abs(beats - t)) <= 0.001 or t >= beats[-1], f"切点 {t:.4f}s 不在拍上"
    # slot 是拍间隔的整数倍
    period = 60.0 / tr["bpm"]
    for s in o.kept():
        assert s.slot / period == pytest.approx(round(s.slot / period), abs=1e-6)


@pytest.mark.integration
def test_uses_at_least_four_clips_all_muted_no_repeated_footage(plan: Plan) -> None:
    o = plan.outputs[0]
    assert len({s.source for s in o.segments}) >= 4
    assert all(s.mute for s in o.segments), "片段自带的声音不要，成片声音是 BGM"
    by_src: dict[str, list] = {}
    for s in o.segments:
        by_src.setdefault(s.source, []).append((s.start, s.end))
    for src, ivs in by_src.items():
        ivs.sort()
        for (a0, a1), (b0, b1) in zip(ivs, ivs[1:]):
            assert b0 >= a1 - 1e-6, f"{src} 里 {a0}-{a1} 与 {b0}-{b1} 重叠：同一段画面不该重复使用"


@pytest.mark.integration
def test_bgm_bed_starts_on_a_downbeat_and_matches_duration(plan: Plan, tr: dict) -> None:
    o = plan.outputs[0]
    (bed,) = o.beds
    assert bed.source == "music" and bed.role == "music" and bed.at == 0.0
    assert min(abs(np.asarray(tr["bar_starts"]) - bed.start)) <= 0.001, f"BGM 起点 {bed.start} 应是小节首拍"
    assert bed.end - bed.start == pytest.approx(o.duration, abs=1e-6)
    assert plan.sources["music"].kind == "audio"


@pytest.mark.integration
def test_segments_carry_reasons_and_bar_beat_details(plan: Plan) -> None:
    o = plan.outputs[0]
    assert all(s.reason in ("beat", "shot") for s in o.segments)
    first = o.segments[0]
    assert first.reason == "beat" and "第 1 小节第 1 拍" in first.detail
    assert any(s.reason == "shot" for s in o.segments)
    # auto：最后一小节每拍切一次，前面每 2 拍切一次
    period = 0.5
    assert [s.slot for s in o.segments[-4:]] == [period] * 4
    assert all(s.slot == pytest.approx(2 * period) for s in o.segments[:-4])


@pytest.mark.integration
def test_adjacent_shots_differ_visually(plan: Plan) -> None:
    divs = [s.evidence["visual_diff"] for s in plan.outputs[0].segments[1:]]
    assert np.mean(divs) > 0.25, "相邻镜头的视觉差异应当大"
    assert all(a.source != b.source for a, b in zip(plan.outputs[0].segments, plan.outputs[0].segments[1:])), "相邻镜头不该来自同一个素材"


@pytest.mark.integration
def test_validates_and_roundtrips(plan: Plan, tmp_path: Path) -> None:
    plan.validate()
    p = plan.save(tmp_path / "a.autocut.json")
    again = Plan.load(p)
    assert again.outputs[0].duration == pytest.approx(plan.outputs[0].duration)
    assert again.outputs[0].beds[0].start == plan.outputs[0].beds[0].start


@pytest.mark.integration
@pytest.mark.parametrize("cut_every,beats", [("beat", 1), ("2beats", 2), ("bar", 4)])
def test_cut_every_modes(clips: list[Path], music: Path, tmp_path: Path, cut_every: str, beats: int) -> None:
    p = M.build(clips, music=music, out=tmp_path / "m.autocut.json", cut_every=cut_every, length=8)
    o = p.outputs[0]
    assert o.duration == pytest.approx(8.0)
    assert all(s.slot == pytest.approx(beats * 0.5) for s in o.segments)
    assert len({s.source for s in o.segments}) >= 4


@pytest.mark.integration
def test_aspect_sets_size_and_center_reframe(clips: list[Path], music: Path, tmp_path: Path) -> None:
    p = M.build(clips, music=music, out=tmp_path / "m.autocut.json", length=6, aspect="9:16")
    o = p.outputs[0]
    assert (o.width, o.height) == (1080, 1920) and o.reframe is not None and o.reframe.mode == "center"


@pytest.mark.integration
def test_not_enough_footage_reuses_and_says_so(clips: list[Path], music: Path, tmp_path: Path) -> None:
    # 2 个 4s 的素材只有 8s 画面，却要 16 个 0.5s 镜头 + 其它 → 够用；换成每拍切、24 秒 = 48 个镜头 → 不够
    p = M.build(clips[:1], music=music, out=tmp_path / "m.autocut.json", cut_every="beat", length=24)
    assert any("不够" in n or "重复" in n for n in p.notes)
    p.validate()


@pytest.mark.integration
def test_music_start_option_snaps_to_bar(clips: list[Path], music: Path, tmp_path: Path) -> None:
    p = M.build(clips, music=music, out=tmp_path / "m.autocut.json", length=6, music_start=5.0)
    start = p.outputs[0].beds[0].start
    assert start in (4.0, 6.0), f"music_start=5.0 应吸附到小节首拍，实际 {start}"


# ------------------------------------------------------------------ 单元


@pytest.mark.unit
def test_beat_grid_regularizes_jittery_detection() -> None:
    rng = np.random.default_rng(0)
    true = np.arange(0, 20, 60 / 128.0)
    beats = true + rng.normal(0, 0.004, len(true))
    res = BeatResult(bpm=127.8, beats=tuple(beats), downbeats=tuple(beats[1::4]), confidence=0.8)
    g = fit_beat_grid(res, 20.0)
    assert g.bpm == 128.0 and g.snapped
    assert g.period == pytest.approx(60 / 128.0)
    assert g.bar_start(0) == pytest.approx(true[1], abs=0.01)  # downbeat 相位取自检测结果
    assert g.n_bars == int((20.0 - g.bar_start(0)) // g.bar)


@pytest.mark.unit
def test_beat_grid_keeps_non_integer_bpm_and_rejects_too_few_beats() -> None:
    beats = tuple(np.arange(0, 12, 60 / 97.5))
    g = fit_beat_grid(BeatResult(bpm=97.5, beats=beats, downbeats=beats[::4], confidence=0.9), 12.0)
    assert not g.snapped and g.bpm == pytest.approx(97.5, abs=0.05)
    with pytest.raises(RecipeError, match="拍点"):
        fit_beat_grid(BeatResult(bpm=0.0, beats=(), downbeats=(), confidence=0.0), 5.0, source_name="noise.wav")


@pytest.mark.unit
def test_plan_bars_picks_nearest_and_prefers_shorter_on_tie() -> None:
    g = BeatGrid(bpm=120, period=0.5, offset=0, bar_k0=0, duration=60, confidence=1, snapped=True)
    assert plan_bars(15.0, g)[0] == 7  # 14s 与 16s 一样近，取短的
    assert plan_bars(15.9, g)[0] == 8
    assert plan_bars(0.3, g)[0] == 1
    with pytest.raises(RecipeError):
        plan_bars(0, g)


@pytest.mark.unit
def test_cut_pattern_accelerates_in_auto_mode() -> None:
    assert M.cut_pattern(3, "auto") == [2, 2, 2, 2, 1, 1, 1, 1]
    assert M.cut_pattern(2, "bar") == [4, 4]
    assert sum(M.cut_pattern(5, "auto")) == 20
    with pytest.raises(RecipeError, match="cut_every"):
        M.cut_pattern(2, "fast")


@pytest.mark.unit
def test_requires_music_and_existing_file(tmp_path: Path) -> None:
    with pytest.raises(RecipeError, match="BGM"):
        M.build([tmp_path / "a.mp4"])
    with pytest.raises(RecipeError, match="找不到 BGM"):
        M.build([tmp_path / "a.mp4"], music=tmp_path / "nope.wav")
