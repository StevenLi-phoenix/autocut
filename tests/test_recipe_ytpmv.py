"""ytpmv 配方：鬼畜材料包 + 第一版对拍。真值来自 fixtures/media/ytpmv.truth.json（120 BPM，12 个已知音节）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pytest

from autocut.analysis import SR, Media
from autocut.audio import frame_db
from autocut.plan import Plan
from autocut.recipes import ytpmv as Y
from autocut.recipes._common import RecipeError

log = logging.getLogger(__name__)

HALF = 0.25  # 120 BPM 的半拍


@pytest.fixture(scope="module")
def src(media: Path) -> Path:
    p = media / "ytpmv" / "source_clip.mp4"
    if not p.exists():
        pytest.skip("缺少 ytpmv 素材：先运行 fixtures/make_fixtures.py")
    return p


@pytest.fixture(scope="module")
def music(media: Path) -> Path:
    return media / "ytpmv" / "music_120bpm.wav"


@pytest.fixture(scope="module")
def tr(media: Path) -> dict:
    return json.loads((media / "ytpmv.truth.json").read_text())


def _iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / (max(a[1], b[1]) - min(a[0], b[0]))


def _loose_truth_words(tr: dict, pad: float = 0.1) -> list[dict]:
    """模拟语音识别的松散时间：每个字前后各多 pad 秒，相邻字首尾相接，标点单独成项。"""
    words = []
    for i, s in enumerate(tr["syllables"]):
        words.append({"text": s["text"], "start": s["start"] - pad, "end": s["end"] + pad, "p": 0.9})
        words.append({"text": "，", "start": s["end"] + pad, "end": s["end"] + pad, "p": 0.9})
    return words


@pytest.fixture(scope="module")
def plan_words(src: Path, music: Path, tr: dict, tmp_path_factory: pytest.TempPathFactory) -> Plan:
    """有文字稿的方案：用带抖动的真值字时间代替语音识别，结果不依赖 whisper 的发挥。"""
    mp = pytest.MonkeyPatch()
    words = _loose_truth_words(tr)
    mp.setattr(Media, "words", lambda self, language=None, track=None: words)
    try:
        return Y.build([src], music=music, out=tmp_path_factory.mktemp("yt") / "y.autocut.json")
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def plan_nowords(src: Path, music: Path, tmp_path_factory: pytest.TempPathFactory) -> Plan:
    mp = pytest.MonkeyPatch()
    mp.setattr(Media, "words", lambda self, language=None, track=None: None)
    try:
        return Y.build([src], music=music, out=tmp_path_factory.mktemp("yt0") / "y.autocut.json")
    finally:
        mp.undo()


# ------------------------------------------------------------------ 集成


@pytest.mark.integration
@pytest.mark.parametrize("which", ["plan_words", "plan_nowords"])
def test_placements_are_on_beats_or_half_beats(which: str, request: pytest.FixtureRequest, tr: dict) -> None:
    plan: Plan = request.getfixturevalue(which)
    o = plan.outputs[0]
    assert len(o.kept()) >= 6, "至少 6 次音节放置"
    beats = np.asarray(tr["beats"])
    halves = np.sort(np.concatenate([beats, beats + HALF]))
    for s, t0, t1 in o.timeline():
        assert np.min(np.abs(halves - t0)) <= 0.001, f"{s.id} 放在 {t0:.4f}s，不在拍或半拍上"
        assert s.reason == "syllable"
    assert o.duration == pytest.approx(4 * 2.0), "默认 4 小节 = 8s"
    assert np.min(np.abs(halves - o.duration)) <= 0.001


@pytest.mark.integration
def test_syllables_overlap_truth_at_least_80_percent(plan_words: Plan, tr: dict) -> None:
    mats = plan_words.options["materials"]
    assert len(mats) == len(tr["syllables"])
    for m, t in zip(mats, tr["syllables"]):
        assert m["text"] == t["text"]
        iou = _iou((m["start"], m["end"]), (t["start"], t["end"]))
        assert iou >= 0.8, f"「{t['text']}」切出 {m['start']}–{m['end']}，真值 {t['start']}–{t['end']}，重合 {iou:.0%}"
    # 放置用的每个片段也是材料里的某个音节
    used = {(s.start, s.end) for s in plan_words.outputs[0].kept()}
    assert used <= {(m["start"], m["end"]) for m in mats}
    for s in plan_words.outputs[0].kept():
        truth = next(t for t in tr["syllables"] if t["text"] == s.text and _iou((s.start, s.end), (t["start"], t["end"])) >= 0.8)
        assert truth


@pytest.mark.integration
def test_without_transcript_still_produces_syllables_and_says_so(plan_nowords: Plan, tr: dict) -> None:
    assert any("没有文字稿" in n for n in plan_nowords.notes)
    mats = plan_nowords.options["materials"]
    assert len(mats) == len(tr["syllables"]), "合成素材的音节之间有明显的谷，按能量也该切出同样多个"
    for m, t in zip(mats, tr["syllables"]):
        assert m["text"] == "" and m["from"] == "energy"
        assert _iou((m["start"], m["end"]), (t["start"], t["end"])) >= 0.8
    assert all(s.text == "" and s.confidence < 0.75 for s in plan_nowords.outputs[0].kept()), "没有文字的音节拿不准，进审阅队列"


@pytest.mark.integration
def test_bar_end_repeats_last_syllable_for_the_ytpmv_feel(plan_words: Plan) -> None:
    segs = plan_words.outputs[0].kept()
    repeats = [s for s in segs if s.evidence.get("repeat")]
    assert len(repeats) >= 4, "每个小节末尾重复最后一个字"
    for r in repeats:
        i = segs.index(r)
        assert (segs[i - 1].start, segs[i - 1].end) == (r.start, r.end)
        assert "重复" in r.detail
    assert all(s.slot is not None and s.slot > 0 for s in segs)


@pytest.mark.integration
def test_bgm_bed_materials_and_first_pass_note(plan_words: Plan) -> None:
    o = plan_words.outputs[0]
    (bed,) = o.beds
    assert bed.source == "music" and bed.role == "music" and bed.end - bed.start == pytest.approx(o.duration)
    assert any("第一版对拍" in n and "剪辑软件" in n for n in plan_words.notes)
    mats = plan_words.options["materials"]
    assert mats and all({"id", "source", "start", "end", "text"} <= set(m) for m in mats)
    assert all(m["source"] in plan_words.sources for m in mats)


@pytest.mark.integration
def test_pitch_option_sets_semitones_on_segments(src: Path, music: Path, tr: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    words = _loose_truth_words(tr)
    monkeypatch.setattr(Media, "words", lambda self, language=None, track=None: words)
    p = Y.build([src], music=music, out=tmp_path / "y.autocut.json", pitch=True)
    pitches = [s.pitch for s in p.outputs[0].kept()]
    assert len(set(pitches)) >= 4 and all(-12 <= x <= 12 for x in pitches)
    assert all("变调" in s.detail for s in p.outputs[0].kept())
    q = Y.build([src], music=music, out=tmp_path / "y2.autocut.json", pitch=False)
    assert all(s.pitch == 0 for s in q.outputs[0].kept())


@pytest.mark.integration
@pytest.mark.parametrize("pattern,per_bar", [("beat", 4), ("half", 8)])
def test_pattern_options_and_length(pattern: str, per_bar: int, src: Path, music: Path, tr: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    words = _loose_truth_words(tr)
    monkeypatch.setattr(Media, "words", lambda self, language=None, track=None: words)
    p = Y.build([src], music=music, out=tmp_path / "y.autocut.json", pattern=pattern, length=4.0)
    o = p.outputs[0]
    assert o.duration == pytest.approx(4.0) and len(o.kept()) == 2 * per_bar
    assert all(s.slot == pytest.approx(0.5 if pattern == "beat" else HALF) for s in o.kept())


@pytest.mark.integration
def test_validates_and_roundtrips(plan_words: Plan, tmp_path: Path) -> None:
    plan_words.validate()
    again = Plan.load(plan_words.save(tmp_path / "y.autocut.json"))
    assert len(again.options["materials"]) == len(plan_words.options["materials"])
    assert again.outputs[0].duration == pytest.approx(plan_words.outputs[0].duration)


@pytest.mark.integration
def test_real_transcript_if_available_still_matches_truth(src: Path, music: Path, tr: dict, tmp_path: Path) -> None:
    """用真正的语音识别（有就测）：文字可能识别错，但音节边界要贴着真值——边界取自能量，不依赖识别的时间。"""
    if Media(src).words() is None:
        pytest.skip("没有可用的语音识别")
    p = Y.build([src], music=music, out=tmp_path / "y.autocut.json")
    mats = p.options["materials"]
    assert len(mats) == len(tr["syllables"]), [m["text"] for m in mats]
    for m, t in zip(mats, tr["syllables"]):
        assert _iou((m["start"], m["end"]), (t["start"], t["end"])) >= 0.8


# ------------------------------------------------------------------ 单元


@pytest.mark.unit
def test_split_valleys_finds_gaps_between_bursts() -> None:
    hop = Y._HOP
    db = np.full(int(2.0 / hop), -60.0)
    for a, b in ((0.1, 0.4), (0.46, 0.8), (1.0, 1.5)):
        db[int(a / hop) : int(b / hop)] = -12.0
    parts = Y.split_valleys(db, 0, len(db))
    assert len(parts) >= 3
    tight = [Y.tighten(db, a * hop, b * hop, a * hop, b * hop) for a, b in parts]
    loud = [t for t in tight if t[2] > -20]
    assert [round(t[0], 2) for t in loud] == [0.1, 0.46, 1.0]


@pytest.mark.unit
def test_split_valleys_ignores_shallow_dips_and_respects_max_parts() -> None:
    hop = Y._HOP
    db = np.full(400, -12.0)
    db[200] = -15.0  # 3dB 的小坑不是音节边界
    assert len(Y.split_valleys(db, 0, 400)) == 1
    db2 = np.full(800, -12.0)
    db2[200:210] = db2[400:410] = db2[600:610] = -60.0
    assert len(Y.split_valleys(db2, 0, 800)) == 4
    assert len(Y.split_valleys(db2, 0, 800, max_parts=2)) == 2


@pytest.mark.unit
def test_pick_run_prefers_loudest_consecutive_window() -> None:
    sy = [Y.Syllable(str(i), i, i + 0.2, peak_db=p) for i, p in enumerate([-30, -30, -10, -10, -12, -30, -30])]
    run = Y.pick_run(sy, 3)
    assert [s.text for s in run] == ["2", "3", "4"]
    assert Y.pick_run(sy, 10) == sy


@pytest.mark.unit
def test_bar_templates_cover_each_bar_with_repeat_at_the_end() -> None:
    for pattern in ("auto", "beat", "half"):
        tpl = Y.bar_templates(pattern, 4)
        assert len(tpl) == 4
        for bar in tpl:
            assert bar[0][0] == 0 and all(0 <= pos < 8 for pos, _ in bar)
            assert [p for p, _ in bar] == sorted(set(p for p, _ in bar))
    auto = Y.bar_templates("auto", 4)
    assert all(bar[-1][1] == bar[-2][1] for bar in auto), "auto 的每个小节最后两次放置是同一个字"
    with pytest.raises(RecipeError, match="pattern"):
        Y.bar_templates("rap", 2)


@pytest.mark.unit
def test_word_based_fallback_when_energy_count_disagrees_with_text() -> None:
    """文字稿是 3 个字，但能量包络只有 2 个谷 → 不能硬配，退回文字稿时间（按能量收紧）。"""
    hop = Y._HOP
    db = np.full(int(2.0 / hop), -60.0)
    db[int(0.2 / hop) : int(0.5 / hop)] = -12.0
    db[int(0.6 / hop) : int(1.0 / hop)] = -12.0
    words = [{"text": "你好吗", "start": 0.1, "end": 1.1, "p": 0.9}]
    out = Y.syllables_from_words(words, db, 2.0, regions=((0.2, 1.0),))
    assert [s.text for s in out] == ["你好吗"]
    assert 0.15 <= out[0].start <= 0.25 and 0.95 <= out[0].end <= 1.05


@pytest.mark.unit
def test_requires_music_and_a_voiced_source(tmp_path: Path, media: Path) -> None:
    with pytest.raises(RecipeError, match="BGM"):
        Y.build([tmp_path / "a.mp4"])
    with pytest.raises(RecipeError, match="找不到 BGM"):
        Y.build([tmp_path / "a.mp4"], music=tmp_path / "nope.wav")
    with pytest.raises(RecipeError, match="带台词"):
        Y.build([], music=media / "ytpmv" / "music_120bpm.wav")
