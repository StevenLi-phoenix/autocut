"""highlights 配方：直播高光切片。真值来自 fixtures/media/stream_vod.truth.json。"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest

from autocut.analysis import Media
from autocut.chat import ChatMessage
from autocut.plan import Plan
from autocut.recipes import highlights as H
from autocut.recipes._common import RecipeError

log = logging.getLogger(__name__)


def _covers(o, h) -> bool:
    segs = o.kept()
    return segs[0].start <= h["start"] and segs[-1].end >= h["end"]


def _inside(t: float, a: float, b: float, tol: float = 0.05) -> bool:
    return a + tol < t < b - tol


# ------------------------------------------------------------------ 集成：stream_vod


@pytest.fixture(scope="module")
def vod(media: Path) -> Path:
    p = media / "stream_vod.mp4"
    if not p.exists():
        pytest.skip("缺少 stream_vod.mp4：先运行 fixtures/make_fixtures.py")
    return p


@pytest.fixture(scope="module")
def tr(media: Path) -> dict:
    import json

    return json.loads((media / "stream_vod.truth.json").read_text())


@pytest.fixture(scope="module")
def plan_chat(vod: Path, media: Path, tmp_path_factory: pytest.TempPathFactory) -> Plan:
    out = tmp_path_factory.mktemp("hl_chat") / "vod.autocut.json"
    return H.build([vod], out=out, chat=media / "stream_vod.chat.jsonl")


@pytest.fixture(scope="module")
def plan_nochat(vod: Path, tmp_path_factory: pytest.TempPathFactory) -> Plan:
    out = tmp_path_factory.mktemp("hl_nochat") / "vod.autocut.json"
    return H.build([vod], out=out)


@pytest.mark.integration
def test_selected_clips_each_cover_a_different_truth_highlight(plan_chat: Plan, tr: dict) -> None:
    sel = [o for o in plan_chat.outputs if o.selected]
    assert len(sel) == 3
    hit = []
    for o in sel:
        idx = [i for i, h in enumerate(tr["highlights"]) if _covers(o, h)]
        assert len(idx) == 1, f"{o.id} {o.kept()[0].start:.1f}–{o.kept()[-1].end:.1f} 应完整覆盖恰好一个 truth 高光，实际 {idx}"
        hit.append(idx[0])
    assert sorted(hit) == [0, 1, 2], "三条切片应分别对应三个不同的高光"


@pytest.mark.integration
def test_lottery_decoy_is_listed_but_not_selected_and_explained(plan_chat: Plan, tr: dict) -> None:
    d = tr["decoys"][0]
    for o in plan_chat.outputs:
        if o.selected:
            a, b = o.kept()[0].start, o.kept()[-1].end
            assert b <= 58.0 or a >= 70.0, f"选中的 {o.id}（{a:.1f}–{b:.1f}）不该落进抽奖刷屏段 {d['start']}–{d['end']}"
    decoys = [o for o in plan_chat.outputs if not o.selected and any("刷屏" in r for r in o.reasons)]
    assert decoys, "被惩罚的抽奖诱饵要作为未选中候选列出来，并在 reasons 里说明"
    assert decoys[0].score < min(o.score for o in plan_chat.outputs if o.selected), "诱饵分数应低于所有选中的切片"
    assert any("刷屏" in n for n in plan_chat.notes), "notes 里也要说明为什么没选"
    assert any("同一句" in r for r in decoys[0].reasons)


@pytest.mark.integration
def test_clip_lengths_within_range_and_structure(plan_chat: Plan) -> None:
    for o in plan_chat.outputs:
        assert 15.0 - 1e-6 <= o.duration <= 30.0 + 1e-6, f"{o.id} 时长 {o.duration:.2f}s 不在 15–30s"
        reasons = [s.reason for s in o.segments]
        assert reasons[-1] == "highlight"
        if len(reasons) == 2:
            assert reasons[0] == "lead_in"
            lead = o.segments[0].end - o.segments[0].start
            assert 3.0 - 1e-6 <= lead <= 10.0 + 1e-6, f"铺垫 {lead:.1f}s 应在 3–10s"
        for a, b in zip(o.segments, o.segments[1:]):
            assert a.end == b.start, "lead_in 与 highlight 首尾相接"
        assert o.title and o.reasons and 0 <= o.score <= 100


@pytest.mark.integration
def test_cut_points_never_inside_speech(plan_chat: Plan, plan_nochat: Plan, tr: dict) -> None:
    spans = [(c["start"], c["end"]) for c in tr["chatter"]] + [(h["start"], h["end"]) for h in tr["highlights"]]
    for plan in (plan_chat, plan_nochat):
        for o in plan.outputs:
            a, b = o.kept()[0].start, o.kept()[-1].end
            for s, e in spans:
                assert not _inside(a, s, e), f"{o.id} 开头 {a:.2f}s 落在 {s}–{e} 的语音中间"
                assert not _inside(b, s, e), f"{o.id} 结尾 {b:.2f}s 落在 {s}–{e} 的语音中间"


@pytest.mark.integration
def test_without_chat_audio_still_finds_at_least_two_highlights(plan_nochat: Plan, tr: dict) -> None:
    found = {i for o in plan_nochat.outputs if o.selected for i, h in enumerate(tr["highlights"]) if _covers(o, h)}
    assert len(found) >= 2, f"没有弹幕时只靠音频应找到 ≥2 个高光，实际 {sorted(found)}"
    assert any("弹幕" in n for n in plan_nochat.notes), "没提供弹幕文件，要在 notes 里说明少了一路信号"
    assert not any("弹幕" in r for o in plan_nochat.outputs for r in o.reasons)


@pytest.mark.integration
def test_vertical_captions_authorization_and_roundtrip(plan_chat: Plan, tmp_path: Path) -> None:
    for o in plan_chat.outputs:
        assert (o.width, o.height) == (1080, 1920)
        assert o.reframe is not None and o.reframe.mode == "center"
        assert o.captions.enabled and o.captions.burn
    assert any("授权" in n for n in plan_chat.notes)
    plan_chat.validate()
    p = plan_chat.save(tmp_path / "x.autocut.json")
    again = Plan.load(p)
    assert [o.id for o in again.outputs] == [o.id for o in plan_chat.outputs]
    assert again.summary()["outputs"][0]["reasons"]


@pytest.mark.integration
def test_degrades_without_transcript_and_says_so(vod: Path, media: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Media, "words", lambda self, language=None, track=None: None)
    plan = H.build([vod], out=tmp_path / "v.autocut.json", chat=media / "stream_vod.chat.jsonl")
    assert any("文字稿" in n for n in plan.notes)
    assert sum(o.selected for o in plan.outputs) == 3
    assert all(o.title for o in plan.outputs)  # 没有文字稿时标题退回时间 + 原因


@pytest.mark.integration
def test_keyword_reasons_and_titles_come_from_transcript(vod: Path, media: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    words = []
    for t0, text in [(33.0, "我去"), (34.0, "这也能秒"), (83.0, "不是吧"), (131.0, "完了完了"), (133.0, "赢了")]:
        words += [{"text": text, "start": t0, "end": t0 + 0.8, "p": 0.9}, {"text": "！", "start": t0 + 0.8, "end": t0 + 0.8, "p": 0.9}]
    monkeypatch.setattr(Media, "words", lambda self, language=None, track=None: words)
    plan = H.build([vod], out=tmp_path / "v.autocut.json", chat=media / "stream_vod.chat.jsonl")
    sel = [o for o in plan.outputs if o.selected]
    assert any("关键词" in r and "我去" in r for o in sel for r in o.reasons)
    assert any("不是吧" in o.title or "我去" in o.title for o in sel)


@pytest.mark.integration
def test_track_reframe_writes_keyframes_inside_frame(vod: Path, media: Path, tmp_path: Path) -> None:
    plan = H.build([vod], out=tmp_path / "v.autocut.json", chat=media / "stream_vod.chat.jsonl", reframe="track", n=2)
    o = next(o for o in plan.outputs if o.selected)
    assert o.reframe.mode == "track" and len(o.reframe.keyframes) >= int(o.duration) - 1
    cw = (9 / 16) / (16 / 9)  # 9:16 在 16:9 里占的宽度比例
    ts = [k[0] for k in o.reframe.keyframes]
    assert ts == sorted(ts) and ts[0] == 0.0
    for _, cx, cy in o.reframe.keyframes:
        assert cw / 2 - 1e-6 <= cx <= 1 - cw / 2 + 1e-6 and cy == 0.5


@pytest.mark.integration
def test_n_and_candidates_options(vod: Path, media: Path, tmp_path: Path) -> None:
    plan = H.build([vod], out=tmp_path / "v.autocut.json", chat=media / "stream_vod.chat.jsonl", n=2, candidates=3)
    assert len(plan.outputs) <= 3 and sum(o.selected for o in plan.outputs) == 2
    scores = [o.score for o in plan.outputs]
    assert scores == sorted(scores, reverse=True), "候选按分数从高到低排"


# ------------------------------------------------------------------ 单元


@pytest.mark.unit
def test_cutter_never_returns_a_point_inside_speech() -> None:
    cut = H.Cutter([(10.0, 14.0), (14.1, 20.0), (30.0, 33.0)], 60.0)
    for target in np.arange(0.0, 60.0, 0.37):
        for lo, hi in ((target - 3, target + 3), (target - 10, target)):
            t, how = cut.snap_start(float(target), lo, hi)
            if how != "hard":
                assert cut.inside(t) is None, f"snap_start({target:.2f}) → {t} 落在语音里"
            t, how = cut.snap_end(float(target), lo, hi)
            if how != "hard":
                assert cut.inside(t) is None, f"snap_end({target:.2f}) → {t} 落在语音里"


@pytest.mark.unit
def test_cutter_snaps_start_to_sentence_start_and_end_after_sentence() -> None:
    cut = H.Cutter([(10.0, 14.0), (30.0, 33.0)], 60.0)
    t, how = cut.snap_start(11.0, 5.0, 12.0)  # 目标落在句子中间：退到句首之前
    assert how == "sentence" and 9.5 <= t < 10.0
    t, how = cut.snap_end(13.0, 12.0, 20.0)  # 落在句子中间：延到句尾 + 0.5s
    assert how == "sentence" and abs(t - 14.5) < 1e-6


@pytest.mark.unit
def test_cutter_falls_back_to_hard_cut_when_speech_never_stops() -> None:
    db = np.full(10000, -30.0)
    db[3500] = -60.0  # 35.00s 是整段说话里最安静的一帧
    cut = H.Cutter([(0.0, 100.0)], 100.0, db=db)
    t, how = cut.snap_start(36.0, 30.0, 40.0)
    assert how == "hard" and abs(t - 35.0) < 0.02


@pytest.mark.unit
def test_protected_spans_split_long_speech_at_sentence_boundaries() -> None:
    words = [
        {"text": "你好", "start": 0.0, "end": 0.6}, {"text": "。", "start": 0.6, "end": 0.6},
        {"text": "再见", "start": 0.7, "end": 1.3}, {"text": "。", "start": 1.3, "end": 1.3},
    ]
    sents = H._sentences(words)
    assert [s[2] for s in sents] == ["你好。", "再见。"]
    spans = H.protected_spans([(0.0, 1.3)], sents)
    assert spans == [(0.0, 0.6), (0.7, 1.3)]  # 一整段连续语音，被句子切成两段，中间 0.1s 可以下刀
    assert H.protected_spans([(0.0, 1.3)], None) == [(0.0, 1.3)]


@pytest.mark.unit
def test_keyword_hits_find_chinese_and_english_with_times() -> None:
    words = [
        {"text": "我去", "start": 1.0, "end": 1.4}, {"text": "，", "start": 1.4, "end": 1.4},
        {"text": "oh", "start": 5.0, "end": 5.2}, {"text": "my", "start": 5.2, "end": 5.4}, {"text": "god", "start": 5.4, "end": 5.7},
    ]
    hits = H._keyword_hits(words)
    assert (1.0, "我去") in hits and (5.0, "oh my god") in hits
    assert H._keyword_hits(None) == []


@pytest.mark.unit
def test_spam_cluster_is_penalized_and_flagged() -> None:
    grid = np.arange(0.0, 120.0, H.HOP)
    gain = np.zeros_like(grid)
    msgs = [ChatMessage(t=50 + i * 0.2, user=f"u{i}", text="1") for i in range(60)]
    msgs += [ChatMessage(t=float(t), user="x", text="哈哈") for t in range(0, 120, 3)]
    msgs.sort(key=lambda m: m.t)
    rate, ratio = H._chat_hot(msgs, grid, 120.0)
    (c,) = H._find_clusters(grid, gain, rate, ratio, msgs, [], [])
    assert c.spam and c.burst > 0.8
    assert c.score == pytest.approx(c.raw_score * H.SPAM_PENALTY)
    assert any("刷屏" in r for r in H.reasons_for(c))


@pytest.mark.unit
def test_diverse_chat_burst_is_not_spam() -> None:
    grid = np.arange(0.0, 120.0, H.HOP)
    words = ["卧槽", "牛", "名场面", "666", "这也行", "太强了", "哈哈哈", "切片"]
    msgs = [ChatMessage(t=50 + i * 0.2, user=f"u{i}", text=words[i % len(words)]) for i in range(60)]
    msgs += [ChatMessage(t=float(t), user="x", text="哈哈") for t in range(0, 120, 3)]
    msgs.sort(key=lambda m: m.t)
    rate, ratio = H._chat_hot(msgs, grid, 120.0)
    (c,) = H._find_clusters(grid, np.zeros_like(grid), rate, ratio, msgs, [], [])
    assert not c.spam and c.score > 30


@pytest.mark.unit
def test_fit_clip_respects_length_bounds_for_many_event_shapes() -> None:
    rng = np.random.default_rng(3)
    for _ in range(60):
        # 随机的说话段（间隔 0.5–6s），随机事件
        spans, t = [], 0.0
        while t < 280:
            t += rng.uniform(0.5, 6.0)
            d = rng.uniform(0.5, 5.0)
            spans.append((t, t + d))
            t += d
        cut = H.Cutter(spans, 300.0)
        a0 = float(rng.uniform(20, 250))
        c = H.Cluster(t0=a0, t1=a0 + 3, a0=a0, a1=a0 + float(rng.uniform(0.5, 20)), peak=a0 + 1, gain=15, audio_driven=True)
        clip = H.fit_clip(c, cut, 300.0, 15.0, 30.0)
        n = clip.end - clip.start
        assert 15.0 - 1e-6 <= n <= 30.0 + 1e-6, f"事件 {a0:.1f}+{c.a1 - c.a0:.1f} → 切片 {clip.start}–{clip.end}（{n:.1f}s）"
        assert clip.start <= clip.t0 <= clip.end
        if clip.hard_cuts == 0:
            assert cut.inside(clip.start) is None and cut.inside(clip.end) is None


@pytest.mark.unit
def test_fit_clip_at_start_and_end_of_file() -> None:
    cut = H.Cutter([], 40.0)
    early = H.fit_clip(H.Cluster(t0=1, t1=3, a0=1.0, a1=3.0, peak=2, gain=15, audio_driven=True), cut, 40.0, 15.0, 30.0)
    assert early.start == 0.0 and early.end - early.start >= 15.0
    late = H.fit_clip(H.Cluster(t0=37, t1=39, a0=37.0, a1=39.0, peak=38, gain=15, audio_driven=True), cut, 40.0, 15.0, 30.0)
    assert late.end <= 40.0 and late.end - late.start >= 15.0 - 1e-6


@pytest.mark.unit
def test_bad_options_give_actionable_errors(tmp_path: Path) -> None:
    with pytest.raises(RecipeError, match="至少要 1 条"):
        H.build([tmp_path / "x.mp4"], n=0)
    with pytest.raises(RecipeError, match="length"):
        H.build([tmp_path / "x.mp4"], length=(30, 15))
    with pytest.raises(RecipeError, match="reframe"):
        H.build([tmp_path / "x.mp4"], reframe="zoom")
    with pytest.raises(RecipeError, match="素材"):
        H.build([])
