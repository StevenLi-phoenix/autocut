"""agent 选段：句段 ID、分页、边界吸附、硬约束校验（合成文字稿，不依赖素材）。"""

from __future__ import annotations

import pytest

from autocut.clips import ClipContext, ClipError, add_clip, build_sentences, check, context_from, fit_bounds, page, resolve_ids
from autocut.plan import Plan, Source

pytestmark = pytest.mark.unit


def words_for(lines: list[tuple[float, float, str]]) -> list[dict]:
    """每句拆成逐词（均分时长），句末带标点——和 whisper 输出同形。"""
    out = []
    for s, e, t in lines:
        toks = t.split()
        step = (e - s) / len(toks)
        for k, w in enumerate(toks):
            out.append({"text": w, "start": round(s + k * step, 3), "end": round(s + (k + 1) * step, 3), "p": 0.9})
    return out


# 四段话：0–20 / 22–50 / 52–95 / 97–110，句间停顿 2s
LINES = [(0.0, 20.0, "Welcome to the show today we talk about cats ."), (22.0, 50.0, "Cats sleep sixteen hours a day and nobody knows why ."),
         (52.0, 95.0, "The real reason is energy saving because hunting is hard work ."), (97.0, 110.0, "That is all for today thanks for watching .")]


@pytest.fixture
def ctx() -> ClipContext:
    sents = build_sentences(words_for(LINES))
    spans = [(s.start, s.end) for s in sents]
    return context_from(sents, spans, 120.0)


def blank_plan() -> Plan:
    return Plan("clips", {"v": Source("v", "v.mp4", "video", 120.0, 1920, 1080, 30.0)}, [])


def test_sentence_ids_are_stable_and_ordered(ctx: ClipContext) -> None:
    assert [s.id for s in ctx.sents] == ["s0001", "s0002", "s0003", "s0004"]
    assert ctx.sents[1].start == pytest.approx(22.0)


def test_page_is_bounded_and_around_centers(ctx: ClipContext) -> None:
    p = page(ctx.sents, offset=0, limit=2)
    assert p["total"] == 4 and p["showing"] == 2 and p["next_offset"] == 2
    p = page(ctx.sents, offset=2, limit=2)
    assert p["next_offset"] is None
    p = page(ctx.sents, around=60.0, limit=2)  # 60s 在第三句里
    assert "s0003" in [i["id"] for i in p["items"]]
    long = build_sentences([{"text": "字" * 400, "start": 0.0, "end": 5.0}])
    assert page(long)["items"][0]["text"].endswith("…")


def test_unknown_id_is_rejected_with_fix(ctx: ClipContext) -> None:
    with pytest.raises(ClipError) as e:
        resolve_ids(ctx, "s0001", "s0099")
    assert e.value.violations[0]["code"] == "unknown_sentence" and "不要自己编时间戳" in e.value.violations[0]["fix"]
    with pytest.raises(ClipError) as e:
        resolve_ids(ctx, "s0003", "s0002")
    assert e.value.violations[0]["code"] == "reversed"


def test_bounds_snap_into_pauses_outside_sentences(ctx: ClipContext) -> None:
    start, hs, end, he = fit_bounds(ctx, 1, 2)  # 第二、三句：22–95
    assert 20.0 < start <= 22.0 and hs in ("sentence", "gap")
    assert 95.0 < end < 97.0 and he in ("sentence", "gap")
    assert ctx.cutter.inside(start) is None and ctx.cutter.inside(end) is None


def test_add_clip_ok_builds_vertical_output_with_burned_captions(ctx: ClipContext) -> None:
    plan = blank_plan()
    out, warns = add_clip(plan, ctx, "s0002", "s0002", title="猫为什么爱睡觉", reason="一个问题直接抛出，自成一体", confidence=0.8, length=(25, 60))
    assert out.id == "clip_1" and (out.width, out.height) == (1080, 1920) and out.captions.burn and out.selected is False
    assert out.segments[0].reason == "highlight" and out.segments[0].evidence["first"] == "s0002"
    assert out.reframe is None or out.reframe.mode == "center"
    assert plan.outputs == [out]


def test_hard_constraints_reject_and_leave_plan_unchanged(ctx: ClipContext) -> None:
    plan = blank_plan()
    with pytest.raises(ClipError) as e:  # s0001 只有 ~20s，短于 30s
        add_clip(plan, ctx, "s0001", "s0001", title="t", reason="r")
    assert {v["code"] for v in e.value.violations} >= {"too_short"} and plan.outputs == []
    with pytest.raises(ClipError) as e:  # s0002–s0003 共 ~75s > 60s
        add_clip(plan, ctx, "s0002", "s0003", title="t", reason="r")
    assert any(v["code"] == "too_long" for v in e.value.violations)


def test_overlap_and_forbidden_and_banned_words(ctx: ClipContext) -> None:
    plan = blank_plan()
    add_clip(plan, ctx, "s0002", "s0002", title="a", reason="r", length=(20, 60))
    with pytest.raises(ClipError) as e:
        add_clip(plan, ctx, "s0002", "s0002", title="again", reason="r", length=(20, 60))
    assert any(v["code"] == "overlaps_other" for v in e.value.violations)
    with pytest.raises(ClipError) as e:
        add_clip(plan, ctx, "s0003", "s0003", title="x", reason="r", length=(20, 60), forbidden=[(60.0, 70.0)])
    assert any(v["code"] == "in_forbidden_zone" for v in e.value.violations)
    with pytest.raises(ClipError) as e:
        add_clip(plan, ctx, "s0003", "s0003", title="x", reason="r", length=(20, 60), banned_words=["energy saving"])
    assert any(v["code"] == "sensitive_word" for v in e.value.violations)


def test_truncated_ending_detected() -> None:
    sents = build_sentences(words_for([(0.0, 40.0, "so the first thing we do is"), (40.6, 80.0, "to open the box .")]))
    # 第一句没有句末标点，与下一句只隔 0.6s（>0.5s 被拆成两句，但 <0.8s 说明话还没说完）→ 半句
    c = context_from(sents, [(s.start, s.end) for s in sents], 100.0)
    start, hs, end, he = fit_bounds(c, 0, 0)
    v = check(c, 0, 0, start, end, hs, he, length=(20, 60))
    assert any(x["code"] == "truncated_ending" for x in v)


def test_late_opening_flagged(ctx: ClipContext) -> None:
    v = check(ctx, 1, 1, 15.0, 55.0, "gap", "gap", length=(20, 60))
    assert any(x["code"] == "late_opening" for x in v)
