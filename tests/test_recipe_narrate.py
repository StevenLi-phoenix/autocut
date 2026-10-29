"""narrate 配方：文稿 + 配图 → 知识视频。真值来自 fixtures/media/knowledge.truth.json。

配音用 macOS `say`（离线）；没有 say 或没有 Tingting 声音时整个集成部分 skip。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from autocut.captions import cues_for
from autocut.plan import Plan
from autocut.recipes import narrate as N
from autocut.recipes._common import RecipeError

log = logging.getLogger(__name__)


def _have_voice(name: str) -> bool:
    if shutil.which("say") is None:
        return False
    r = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
    return any(ln.startswith(name) for ln in r.stdout.splitlines())


needs_say = pytest.mark.skipif(not _have_voice("Tingting"), reason="需要 macOS say 的 Tingting 声音")


@pytest.fixture(scope="module")
def kdir(media: Path) -> Path:
    d = media / "knowledge"
    if not (d / "script.md").exists():
        pytest.skip("缺少 knowledge 素材：先运行 fixtures/make_fixtures.py")
    return d


@pytest.fixture(scope="module")
def tr(media: Path) -> dict:
    return json.loads((media / "knowledge.truth.json").read_text())


@pytest.fixture(scope="module")
def plan(kdir: Path, tmp_path_factory: pytest.TempPathFactory) -> Plan:
    work = tmp_path_factory.mktemp("nr")
    mp = pytest.MonkeyPatch()
    mp.setenv("AUTOCUT_CACHE", str(work / "cache"))
    try:
        return N.build([kdir / "script.md", kdir / "img1.png", kdir / "img2.png", kdir / "img3.png"], out=work / "k.autocut.json")
    finally:
        mp.undo()


# ------------------------------------------------------------------ 集成


@needs_say
@pytest.mark.integration
def test_narration_duration_within_15_percent_of_reference(plan: Plan, tr: dict) -> None:
    ref = tr["reference_narration_seconds"]
    dur = plan.sources["narration"].duration
    assert ref * 0.85 <= dur <= ref * 1.15, f"配音 {dur:.1f}s，参考 {ref}s"
    assert plan.outputs[0].duration == pytest.approx(dur, abs=0.01), "成片时长 = 配音时长"


@needs_say
@pytest.mark.integration
def test_caption_source_has_one_item_per_sentence_in_exact_times(plan: Plan, tr: dict) -> None:
    src = plan.sources["narration"]
    assert src.kind == "audio" and src.words
    p = Path(src.words)
    if not p.is_absolute():
        p = plan.path.parent / p
    words = json.loads(p.read_text())
    assert len(words) == 6 == len(tr["sentences"])
    assert [w["text"] for w in words] == tr["sentences"]
    for a, b in zip(words, words[1:]):
        assert a["start"] < a["end"] <= b["start"], "逐句时间递增、互不重叠"
    assert words[-1]["end"] <= src.duration
    assert all(set(w) >= {"text", "start", "end", "p"} for w in words)


@needs_say
@pytest.mark.integration
def test_sentence_times_match_the_actual_audio(plan: Plan) -> None:
    """每句的起止要贴着配音里真正有声音的位置：句内有声，句间的停顿里没有。"""
    import numpy as np

    from autocut.audio import frame_db
    from autocut.analysis import Media

    src = plan.sources["narration"]
    wav = plan.source_path("narration")
    db = frame_db(Media(wav).audio(), 16000, 0.01, 0.025)
    p = Path(src.words) if Path(src.words).is_absolute() else plan.path.parent / src.words
    words = json.loads(p.read_text())
    for w in words:
        seg = db[int(w["start"] / 0.01) : int(w["end"] / 0.01)]
        assert np.percentile(seg, 90) > -45, f"「{w['text']}」的时间段里应该有声音"
    for a, b in zip(words, words[1:]):
        gap = db[int((a["end"] + 0.06) / 0.01) : int((b["start"] - 0.03) / 0.01)]
        assert len(gap) == 0 or gap.max() < -50, "句间停顿应当是静音"


@needs_say
@pytest.mark.integration
def test_all_images_used_and_each_held_at_least_2_5_seconds(plan: Plan) -> None:
    o = plan.outputs[0]
    segs = o.kept()
    assert {s.source for s in segs} == {"img1", "img2", "img3"}
    for s in segs:
        assert plan.sources[s.source].kind == "image"
        assert s.slot >= 2.5, f"{s.source} 只停留 {s.slot:.2f}s"
        assert s.reason == "image" and s.detail and s.text
    assert [s.source for s in segs] == ["img1", "img2", "img3"], "按给定顺序使用"
    # 画面首尾相接，覆盖全部配音
    assert sum(s.slot for s in segs) == pytest.approx(plan.sources["narration"].duration, abs=0.01)


@needs_say
@pytest.mark.integration
def test_opening_hook_is_the_first_question_in_big_text(plan: Plan) -> None:
    o = plan.outputs[0]
    (hook,) = [x for x in o.overlays if x.kind == "text"]
    assert hook.at == 0.0 and 2.0 <= hook.duration <= 3.0
    assert hook.text == "为什么天空是蓝色的？"
    assert any("开头钩子" in n for n in plan.notes)


@needs_say
@pytest.mark.integration
def test_voice_captions_and_bed_wiring(plan: Plan) -> None:
    o = plan.outputs[0]
    assert (o.width, o.height) == (1920, 1080)
    assert o.captions.enabled and o.captions.burn and o.captions.source == "narration"
    (bed,) = [b for b in o.beds if b.role == "narration"]
    assert bed.source == "narration" and bed.at == 0.0 and bed.end == pytest.approx(plan.sources["narration"].duration, abs=0.01)
    assert plan.options["voice"] == "Tingting"
    cues = cues_for(plan, o)  # 字幕模块能从这份逐句文字稿生成字幕
    assert len(cues) == 6 and cues[0].text.startswith("为什么天空")


@needs_say
@pytest.mark.integration
def test_validates_and_roundtrips(plan: Plan, tmp_path: Path) -> None:
    plan.validate()
    again = Plan.load(plan.save(tmp_path / "k.autocut.json"))
    assert [s.slot for s in again.outputs[0].kept()] == [s.slot for s in plan.outputs[0].kept()]
    assert again.sources["narration"].words


@needs_say
@pytest.mark.integration
def test_music_bed_ducks_under_narration(kdir: Path, media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))
    music = media / "montage" / "music_120bpm.wav"
    p = N.build([kdir / "script.md", kdir / "img1.png", kdir / "img2.png"], out=tmp_path / "k.autocut.json", music=music)
    beds = {b.role: b for b in p.outputs[0].beds}
    assert beds["music"].duck_db == 12 and beds["music"].source == "music"
    assert beds["music"].end <= p.sources["music"].duration + 1e-6
    assert any("BGM" in n for n in p.notes), "音乐（24s）比成片短，要提醒"


@needs_say
@pytest.mark.integration
def test_too_many_images_for_the_duration_drops_extras_with_a_note(kdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))
    s = tmp_path / "short.md"
    s.write_text("这是第一句话。这是第二句话。\n")
    imgs = [kdir / f"img{i}.png" for i in (1, 2, 3)]
    p = N.build([s, *imgs], out=tmp_path / "s.autocut.json")
    used = {x.source for x in p.outputs[0].kept()}
    assert len(used) < 3 and all(x.slot >= 2.5 - 1e-6 for x in p.outputs[0].kept())
    assert any("没用上" in n for n in p.notes)


@needs_say
@pytest.mark.integration
def test_image_directives_in_script_pin_images_to_sentences(kdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))
    lines = (kdir / "script.md").read_text().splitlines()
    # 标题 + 6 句；指定 img1 从第 1 句、img3 从第 3 句、img2 从第 5 句
    body = [ln for ln in lines if ln and not ln.startswith("#")]
    text = "# 标题\n\n![a](img1.png)\n" + body[0] + "\n" + body[1] + "\n![b](img3.png)\n" + "\n".join(body[2:4]) + "\n![c](img2.png)\n" + "\n".join(body[4:]) + "\n"
    s = tmp_path / "pinned.md"
    s.write_text(text)
    p = N.build([s, kdir / "img1.png", kdir / "img2.png", kdir / "img3.png"], out=tmp_path / "p.autocut.json")
    segs = p.outputs[0].kept()
    assert [x.source for x in segs] == ["img1", "img3", "img2"]
    assert [x.evidence["sentences"] for x in segs] == [[1, 2], [3, 4], [5, 6]]
    assert all(x.confidence >= 0.9 for x in segs), "作者明确指定的，把握度高"


@needs_say
@pytest.mark.integration
def test_english_script_uses_english_voice_and_no_hook_note(tmp_path: Path, kdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not _have_voice("Samantha"):
        pytest.skip("需要 Samantha 声音")
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))
    s = tmp_path / "en.md"
    s.write_text("Light from the sun looks white. It is actually a mix of many colors. Blue light scatters the most in air.\n")
    p = N.build([s, kdir / "img1.png"], out=tmp_path / "e.autocut.json")
    assert p.options["voice"] == "Samantha"
    assert not p.outputs[0].overlays
    assert any("没有大字钩子" in n for n in p.notes)


# ------------------------------------------------------------------ 单元


@pytest.mark.unit
def test_parse_script_splits_sentences_and_skips_markup() -> None:
    sc = N.parse_script(
        "---\ntitle: x\n---\n# 标题\n\n![图](a.png)\n**加粗**的第一句。第二句！\n- 列表项一\n\n"
        "[链接文字](http://x.y)第三句？Hello Mr. Smith. It works.\n<!-- 注释 -->\n```\ncode\n```\n"
    )
    assert sc.title == "标题"
    assert sc.sentences == ["加粗的第一句。", "第二句！", "列表项一", "链接文字第三句？", "Hello Mr. Smith.", "It works."]
    assert sc.image_at == {"a.png": 0}
    assert sc.paragraph_end == [False, False, True, False, False, True]


@pytest.mark.unit
def test_parse_script_keeps_closing_quotes_and_ellipsis_with_sentence() -> None:
    sc = N.parse_script("他说“走吧！”然后离开了。等一下……真的吗?\n")
    assert sc.sentences == ["他说“走吧！”", "然后离开了。", "等一下……", "真的吗?"]
    assert N.parse_script("# 只有标题\n").sentences == []


@pytest.mark.unit
def test_detect_voice_by_script_language() -> None:
    assert N.detect_voice(["今天天气很好。"]) == "Tingting"
    assert N.detect_voice(["The weather is nice."]) == "Samantha"
    assert N.detect_voice(["Python 是一门语言。"]) == "Tingting"


@pytest.mark.unit
def test_hook_prefers_question_then_title_then_none() -> None:
    q = N.Script("T", ["为什么会这样？", "因为……"], [False, True], {})
    assert N.hook_for(q) == ("为什么会这样？", "第一句是个问题")
    t = N.Script("今天讲光", ["光是一种波。"], [True], {})
    assert N.hook_for(t) == ("今天讲光", "文稿标题")
    assert N.hook_for(N.Script(None, ["光是一种波。"], [True], {})) is None
    assert N.hook_for(N.Script(None, ["Why is the sky blue"], [True], {}))[1] == "第一句是个问题"


@pytest.mark.unit
def test_partition_balances_groups_and_respects_min_hold() -> None:
    bounds = [0, 2, 8, 14, 20, 26, 36]  # 6 句，时长 2 6 6 6 6 10
    starts = N.partition(bounds, 3, 2.5)
    assert starts is not None and starts[0] == 0 and len(starts) == 3
    durs = [bounds[b] - bounds[a] for a, b in zip(starts, starts[1:] + [6])]
    assert min(durs) >= 2.5 and max(durs) - min(durs) <= 8
    assert N.partition(bounds, 6, 2.5) is None, "第 1 句只有 2s，6 组不可能每组 ≥2.5s"
    assert N.partition(bounds, 7, 2.5) is None
    assert N.partition(bounds, 1, 2.5) == [0]


@pytest.mark.unit
def test_partition_is_optimal_for_a_small_case() -> None:
    import itertools

    bounds = [0, 3, 4, 9, 10, 17, 18, 24]
    n = len(bounds) - 1
    for k in (2, 3):
        best = min(
            (c for c in itertools.combinations(range(1, n), k - 1)),
            key=lambda cut: sum((d - (bounds[n] / k)) ** 2 for d in _durs(bounds, (0, *cut, n)) ) if min(_durs(bounds, (0, *cut, n))) >= 2.5 else 1e9,
        )
        got = N.partition(bounds, k, 2.5)
        assert got == [0, *best]


def _durs(bounds: list[float], idx: tuple[int, ...]) -> list[float]:
    return [bounds[b] - bounds[a] for a, b in zip(idx, idx[1:])]


@pytest.mark.unit
def test_input_validation_errors_are_actionable(tmp_path: Path) -> None:
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    with pytest.raises(RecipeError, match="恰好一个文稿"):
        N.build([img])
    s = tmp_path / "s.md"
    s.write_text("你好。\n")
    with pytest.raises(RecipeError, match="至少一张图片"):
        N.build([s])
    with pytest.raises(RecipeError, match="不认识"):
        N.build([s, tmp_path / "x.xyz"])
    e = tmp_path / "e.md"
    e.write_text("# 只有标题\n")
    with pytest.raises(RecipeError, match="没有可朗读的句子"):
        N.build([e, img])
