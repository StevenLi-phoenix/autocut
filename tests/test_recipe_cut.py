"""recipes/cut：口播 / 录课 / 游戏实况去废片。直接拿方案和 truth.json 对照，不渲染。"""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from autocut import transcribe
from autocut.analysis import Media
from autocut.plan import Plan
from autocut.recipes import cut
from autocut.recipes._common import RecipeError
from conftest import coverage

log = logging.getLogger(__name__)

needs_asr = pytest.mark.skipif(not transcribe.available(), reason=transcribe.unavailable_reason() or "")
EDGE_TOL = 0.02  # 剪口离 speech 边缘的容差


@pytest.fixture(scope="module", autouse=True)
def asr_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """本次测试会话共用的分析缓存（与 test_transcribe 共享，同一素材只识别一遍）。"""
    d = tmp_path_factory.getbasetemp() / "autocut-asr-cache"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AUTOCUT_CACHE", str(d))
        yield d


_plans: dict[tuple, Plan] = {}


def _build(media: Path, name: str, **kw: Any) -> Plan:
    key = (name, tuple(sorted(kw.items())))
    if key not in _plans:
        _plans[key] = cut.build([media / f"{name}.mp4"], **kw)
    return _plans[key]


# ------------------------------------------------------------------ 对照 truth 的工具


def _kept(plan: Plan) -> list[tuple[float, float]]:
    return [(s.start, s.end) for s in plan.outputs[0].segments if s.action == "keep"]


def _cut_by(plan: Plan, reason: str) -> list[tuple[float, float]]:
    return [(s.start, s.end) for s in plan.outputs[0].segments if s.action == "cut" and s.reason == reason]


def _cut_edges(plan: Plan) -> list[float]:
    """剪/留切换处的时刻（真正会听到剪口的地方）。"""
    segs = plan.outputs[0].segments
    return [b.start for a, b in zip(segs, segs[1:]) if a.action != b.action]


def _check_contract(plan: Plan, duration: float) -> None:
    """覆盖整个素材、首尾相接、每段都有理由/把握度/说明。"""
    segs = plan.outputs[0].segments
    assert segs[0].start == 0.0
    assert segs[-1].end == pytest.approx(duration, abs=0.01)
    for a, b in zip(segs, segs[1:]):
        assert a.end == b.start, (a.id, b.id)
        assert a.source == b.source
    assert any(s.action == "cut" for s in segs)
    for s in segs:
        assert s.reason and s.detail and 0 <= s.confidence <= 1, s
    assert len({s.id for s in segs}) == len(segs)
    plan.validate()


# ------------------------------------------------------------------ 口播 / 录课


@pytest.mark.integration
@pytest.mark.parametrize("name", ["talk_zh", "lecture_en"])
def test_talk_and_lecture_natural(media: Path, truth, name: str) -> None:
    plan = _build(media, name)
    tr = truth(name)
    _check_contract(plan, tr["duration"])
    kept, edges = _kept(plan), _cut_edges(plan)
    keep_len = cut.PACES["natural"]
    for s in tr["segments"]:
        a, b = s["start"], s["end"]
        if s["kind"] == "speech" and "tag" not in s:
            cov = coverage(kept, a, b) / (b - a)
            assert cov >= 0.95, (name, s, cov)
            inside = [t for t in edges if a + EDGE_TOL < t < b - EDGE_TOL]
            assert not inside, (name, s, inside)  # 剪口绝不落在说话中间（A2）
        elif s["kind"] == "gap" and b - a >= 1.0:
            k = coverage(kept, a, b)
            assert k <= keep_len + 0.15, (name, s, k)
    log.info("%s: %s", name, plan.summary()["outputs"][0])


@pytest.mark.integration
@needs_asr
@pytest.mark.parametrize("name", ["talk_zh", "lecture_en"])
def test_retakes_and_fillers_cut(media: Path, truth, name: str) -> None:
    plan = _build(media, name)
    tagged = [s for s in truth(name)["segments"] if s.get("tag") in ("retake", "filler")]
    assert {s["tag"] for s in tagged} == {"retake", "filler"}
    for s in tagged:
        a, b = s["start"], s["end"]
        cov = coverage(_cut_by(plan, s["tag"]), a, b) / (b - a)
        assert cov >= 0.95, (name, s, cov)
    retake = next(x for x in plan.outputs[0].segments if x.reason == "retake")
    assert "取代" in retake.detail and retake.confidence >= 0.75 and retake.text
    assert plan.notes == []
    src = next(iter(plan.sources.values()))
    assert src.words and Path(src.words).exists()
    cap = plan.outputs[0].captions
    assert cap.enabled and not cap.burn


@pytest.mark.integration
@pytest.mark.parametrize("name", ["talk_zh", "lecture_en"])
def test_pace_is_monotonic(media: Path, name: str) -> None:
    d = [_build(media, name, pace=p).outputs[0].duration for p in ("tight", "natural", "relaxed")]
    log.info("%s 时长 tight/natural/relaxed = %s", name, d)
    assert d[0] < d[1] < d[2]


@pytest.mark.integration
def test_default_plan_path_and_sources(media: Path) -> None:
    plan = _build(media, "talk_zh")
    assert plan.path == (media / "talk_zh.autocut.json").resolve()
    src = plan.sources["talk_zh"]
    assert src.path == "talk_zh.mp4" and src.duration and src.width == 640
    assert plan.recipe == "cut" and plan.options["pace"] == "natural"


# ------------------------------------------------------------------ 游戏实况


@pytest.mark.integration
def test_game_commentary(media: Path, truth) -> None:
    plan = _build(media, "game_commentary")
    tr = truth("game_commentary")
    _check_contract(plan, tr["duration"])
    src = plan.sources["game_commentary"]
    assert src.analysis_track == 1 and src.audio_track == 0  # mic 判断，mix 出声
    kept, segs = _kept(plan), plan.outputs[0].segments
    for s in tr["segments"]:
        a, b = s["start"], s["end"]
        if s["kind"] == "speech":
            assert coverage(kept, a, b) / (b - a) >= 0.99, s
        elif s.get("tag") == "action":
            assert coverage(kept, a, b) / (b - a) >= 0.9, s
            acts = [x for x in segs if x.reason == "action" and x.action == "keep" and x.start < b and x.end > a]
            assert acts and 0.6 <= acts[0].confidence < 0.8 and "爆发" in acts[0].detail, acts
        elif s.get("tag") == "boring":
            assert coverage(kept, a, b) <= 1.0, s
            assert any(x.reason == "idle" and x.action == "cut" and x.start < b and x.end > a for x in segs)


# ------------------------------------------------------------------ 降级、保护区间、多素材


@pytest.mark.integration
def test_no_model_still_cuts_pauses(media: Path, truth, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "available", lambda: False)
    plan = cut.build([media / "talk_zh.mp4"])
    assert any("只剪了停顿" in n and "重说和口头禅未处理" in n for n in plan.notes), plan.notes
    reasons = {s.reason for s in plan.outputs[0].segments if s.action == "cut"}
    assert reasons == {"silence"}
    kept = _kept(plan)
    for s in truth("talk_zh")["segments"]:
        if s["kind"] == "speech":
            assert coverage(kept, s["start"], s["end"]) / (s["end"] - s["start"]) >= 0.95
        elif s["end"] - s["start"] >= 1.0:
            assert coverage(kept, s["start"], s["end"]) <= cut.PACES["natural"] + 0.15
    assert next(iter(plan.sources.values())).words is None


@pytest.mark.integration
def test_no_model_note_lists_search_paths(media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "REPO_MODELS", tmp_path / "none-a")
    monkeypatch.setattr(transcribe, "USER_MODELS", tmp_path / "none-b")
    monkeypatch.delenv(transcribe.ENV_MODEL, raising=False)
    plan = cut.build([media / "lecture_en.mp4"])
    note = " ".join(plan.notes)
    assert "没有找到语音识别模型" in note and str(tmp_path / "none-a") in note


@pytest.mark.integration
def test_asr_failure_is_reported(media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path))  # 新缓存，保证真的去调用识别
    monkeypatch.setattr(transcribe, "available", lambda: True)

    def boom(*a: object, **k: object) -> None:
        raise transcribe.TranscribeError("whisper-cli 失败（测试）")

    monkeypatch.setattr(transcribe, "transcribe", boom)
    plan = cut.build([media / "talk_zh.mp4"])
    assert any("语音识别失败" in n and "只剪了停顿" in n for n in plan.notes), plan.notes


@pytest.mark.integration
@needs_asr
def test_protect_ranges(media: Path, truth) -> None:
    retake = [s for s in truth("talk_zh")["segments"] if s.get("tag") == "retake"]
    a, b = retake[0]["start"] - 0.1, retake[-1]["end"] + 0.1
    plan = cut.build([media / "talk_zh.mp4"], protect=[(a, b), (9.6, 10.6)])
    segs = plan.outputs[0].segments
    for lo, hi in ((a, b), (9.6, 10.6)):
        over = [s for s in segs if s.start < hi and s.end > lo]
        assert over and all(s.action == "keep" and s.locked for s in over), over
    assert any(s.reason == "manual" and "原判断" in s.detail for s in segs)
    _check_contract(plan, truth("talk_zh")["duration"])


@pytest.mark.integration
def test_multiple_sources(media: Path) -> None:
    plan = cut.build([media / "talk_zh.mp4", media / "lecture_en.mp4"])
    assert list(plan.sources) == ["talk_zh", "lecture_en"]
    segs = plan.outputs[0].segments
    for sid, src in plan.sources.items():
        mine = [s for s in segs if s.source == sid]
        assert mine[0].start == 0.0 and mine[-1].end == pytest.approx(src.duration, abs=0.01)
        assert all(x.end == y.start for x, y in zip(mine, mine[1:]))


@pytest.mark.integration
def test_build_from_scratch_is_fast(media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    caplog: pytest.LogCaptureFixture) -> None:
    """talk_zh 清空缓存后完整跑一遍（含语音识别）不超过 15s，耗时写进日志。"""
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "fresh"))
    caplog.set_level(logging.INFO, logger="autocut")
    t0 = time.perf_counter()
    cut.build([media / "talk_zh.mp4"])
    dt = time.perf_counter() - t0
    log.info("talk_zh 从零开始 build：%.2fs", dt)
    assert dt <= 15.0
    assert any("cut 完成" in r.getMessage() and "耗时" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ 合成：词间没有停顿时的剪口


def _write_wav(path: Path, x: np.ndarray, sr: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


@pytest.mark.unit
def test_filler_without_pause_cuts_at_energy_dips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """「今天，嗯，我们」连着说（词间只有 60ms 的能量低谷，不构成停顿）：剪口必须落在低谷里，不落在字上。"""
    sr, rng = 16000, np.random.default_rng(0)
    layout = [("s", 0.5), ("v", 0.5), ("d", 0.06), ("v", 0.30), ("d", 0.06), ("v", 0.58), ("s", 1.5),
              ("v", 0.5), ("s", 0.5)]  # s=静音 v=说话 d=低谷
    level = {"s": 10 ** (-60 / 20), "v": 10 ** (-18 / 20), "d": 10 ** (-45 / 20)}
    x = np.concatenate([rng.standard_normal(int(d * sr)) * level[k] for k, d in layout])
    wav = _write_wav(tmp_path / "talk.wav", x)
    # 低谷：[1.0, 1.06]、[1.36, 1.42]；whisper 给的词边界偏早约 40ms（t_dtw 的系统偏差）
    words = [
        {"text": "今天", "start": 0.5, "end": 0.96, "p": 1.0}, {"text": "，", "start": 0.96, "end": 0.96, "p": 1.0},
        {"text": "嗯", "start": 0.96, "end": 1.32, "p": 0.7}, {"text": "，", "start": 1.32, "end": 1.32, "p": 1.0},
        {"text": "我们", "start": 1.32, "end": 2.0, "p": 1.0}, {"text": "开始", "start": 3.5, "end": 4.0, "p": 1.0},
    ]
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(transcribe, "available", lambda: True)
    monkeypatch.setattr(Media, "words", lambda self, language=None, track=None: words)
    plan = cut.build([wav])
    segs = plan.outputs[0].segments
    filler = [s for s in segs if s.reason == "filler"]
    assert len(filler) == 1 and filler[0].action == "cut", segs
    f = filler[0]
    assert 1.0 - 0.011 <= f.start <= 1.06 + 0.011, f
    assert 1.36 - 0.011 <= f.end <= 1.42 + 0.011, f
    assert f.confidence == 0.8 and "能量最低" in f.detail
    # 1.5s 的停顿被剪到 0.4s，剪口不碰说话
    long_gap = coverage(_kept(plan), 2.0, 3.5)
    assert long_gap == pytest.approx(0.4, abs=0.1)
    for t in _cut_edges(plan):
        assert not (0.5 < t < 1.0 or 1.06 < t < 1.36 or 1.42 < t < 2.0 or 3.5 < t < 4.0), t


# ------------------------------------------------------------------ 文字判断（纯文字，不看音频）


def _ws(text: str) -> list[dict[str, Any]]:
    """把一段文字做成逐词文字稿（judge 的输入）：中文每字一词、英文按空格；标点并入前一个词的 after。"""
    import re

    ws: list[dict[str, Any]] = []
    t = 0.0
    for tok in re.findall(r"[A-Za-z']+|[一-鿿]|[^\sA-Za-z'一-鿿]", text):
        if re.match(r"[A-Za-z'一-鿿]", tok):
            ws.append({"text": tok, "start": t, "end": t + 0.2, "p": 1.0, "after": ""})
            t += 0.2
        elif ws:
            ws[-1]["after"] += tok
            t += 0.3  # 标点处停一下
    return ws


def _cut_text(text: str) -> list[tuple[str, str, str]]:
    ws = _ws(text)
    sep = " " if any(c.isascii() and c.isalpha() for c in text) else ""
    return [(d.action, d.reason, sep.join(w["text"] for w in ws[d.i0: d.i1 + 1])) for d in cut.judge(ws)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 有重来标记：标记和被放弃的那遍一起剪
        ("第二步是把没用的停顿。不对，我重新说一遍。第二步，是把没有用的停顿全部删掉。",
         [("cut", "retake", "第二步是把没用的停顿不对我重新说一遍")]),
        ("The gulf of execution is the gap between. Sorry, let me say that again. "
         "The gulf of execution is the gap between what you want and what you get.",
         [("cut", "retake", "The gulf of execution is the gap between Sorry let me say that again")]),
        # 没有标记，但后一句开头重复前一句、而且说得更多
        ("第二步是，第二步是把没用的停顿删掉。", [("cut", "retake", "第二步是")]),
        # 排比句：开头相似但长度相近，不是重说
        ("The gulf of execution is the gap between what you want and what the interface lets you do. "
         "The gulf of evaluation is the gap between what the system shows and what you understand.", []),
        ("我们今天讲第一个问题。我们今天讲第二个问题。", []),
        # 弱标记又找不到相似的重说：正常说话
        ("你觉得是甲吗？不对，是乙。", []),
        # 单独出现的口头禅剪掉；连在词里的不动；歧义词只标记
        ("首先准备素材，嗯，然后打开软件。", [("cut", "filler", "嗯")]),
        ("然后嗯打开软件。", []),
        ("Um, both ideas come from Don Norman.", [("cut", "filler", "Um")]),
        ("那个，我们开始吧。", [("keep", "filler", "那个")]),
    ],
)
def test_judge(text: str, expected: list[tuple[str, str, str]]) -> None:
    assert _cut_text(text) == expected


@pytest.mark.unit
def test_judge_confidence_and_detail() -> None:
    ds = cut.judge(_ws("那个，我们开始吧。第二步是，第二步是把没用的停顿删掉。"))
    amb, retake = ds
    assert amb.action == "keep" and amb.confidence < 0.75 and "没有剪" in amb.detail
    assert retake.confidence < 0.95 and "保留后一遍" in retake.detail and retake.evidence["similarity"] >= 0.6
    assert cut.judge(_ws("第二步是，第二步是把没用的停顿删掉。"), retakes=False) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kw", "msg"),
    [({"pace": "fast"}, "tight"), ({"pace": 30}, "0–10"), ({"protect": [(5, 2)]}, "开始 < 结束"),
     ({"protect": ["abc"]}, "开始秒"), ({"keep_action": "maybe"}, "keep_action")],
)
def test_bad_options(kw: dict[str, Any], msg: str, tmp_path: Path) -> None:
    with pytest.raises(RecipeError, match=msg):
        cut.build([tmp_path / "x.mp4"], **kw)


@pytest.mark.unit
def test_pace_seconds() -> None:
    assert cut._pace_seconds("Tight") == 0.15 and cut._pace_seconds(0.3) == 0.3
    assert cut._sec(0.4) == "0.4s" and cut._sec(0.15) == "0.15s" and cut._sec(2.6) == "2.6s"
