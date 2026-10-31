"""worker 流水线集成测试：真 autocut + 合成素材（fixtures/media，缺失时 skip）。"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from autocut.worker import delivery, ingest
from autocut.worker.config import WorkerConfig
from autocut.worker.server import App

pytestmark = pytest.mark.integration


@pytest.fixture
def app(tmp_path: Path, media: Path) -> App:
    root = tmp_path / "root"
    root.mkdir()
    from autocut.worker import pipelines

    a = App(WorkerConfig(root=root, nice=0, port=0))
    pipelines.register(a)
    return a


def wait(app: App, job: str, run: str, t: float = 600.0) -> dict:
    end = time.time() + t
    while time.time() < end:
        r = app.runner.get(job, run, wait=2)
        if r["status"] in ("ok", "failed", "cancelled"):
            return r
    raise AssertionError("超时")


def test_cut_pipeline_end_to_end(app: App, media: Path) -> None:
    job = app.store.create_job("cut", {"attribution": {"repost_label": True}})["id"]
    shutil.copy(media / "talk_zh.mp4", app.store.job_dir(job) / "src" / "talk_zh.mp4")
    ingest.register_source(app.store, job, {"kind": "url", "file": "src/talk_zh.mp4", "url": "https://example.com/v", "title": "测试", "channel": "某人"})
    run = app.pipelines["cut"](app, job, {})
    r = wait(app, job, run["id"])
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    assert res["state"] == "awaiting_confirmation"
    d = app.store.job_dir(job)
    dv = res["deliverables"][0]
    assert (d / dv["final"]).stat().st_size > 10_000
    assert (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES
    assert "转载自 某人《测试》" in res["summary"]
    assert [s["name"] for s in r["stages"]] == ["cut", "guard", "check-plan", "render", "preview"]
    assert delivery.state(app.store, job)["status"] == "awaiting_confirmation"


def test_cut_pipeline_cancel(app: App, media: Path) -> None:
    from autocut.worker import pipeline

    job = app.store.create_job("cut", {})["id"]
    shutil.copy(media / "talk_zh.mp4", app.store.job_dir(job) / "src" / "talk_zh.mp4")
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/talk_zh.mp4"})
    run = app.pipelines["cut"](app, job, {})
    time.sleep(1.0)
    assert pipeline.cancel(app, job, run["id"])
    r = wait(app, job, run["id"], 60)
    assert r["status"] == "cancelled"


def test_cut_pipeline_without_source_fails_with_fix(app: App) -> None:
    job = app.store.create_job("cut", {})["id"]
    r = wait(app, job, app.pipelines["cut"](app, job, {})["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "no_source" and r["result"]["error"]["fix"]


def test_guard_uncertain_restores_low_confidence_cuts(tmp_path: Path) -> None:
    from autocut.plan import Output, Plan, Segment, Source
    from autocut.worker.pipe_cut import guard_uncertain

    segs = [Segment("d1", "a", 0, 2, "keep", "speech", 1.0), Segment("d2", "a", 2, 4, "cut", "silence", 0.5),
            Segment("d3", "a", 4, 6, "cut", "silence", 0.9), Segment("d4", "a", 6, 7, "cut", "retake", 0.7, locked=True),
            Segment("d5", "a", 7, 8, "cut", "silence", 0.95, detail="停顿 0.9s；停在句子中间")]
    plan = Plan("cut", {"a": Source("a", "a.mp4", "video", 8.0)}, [Output("main", segs)])
    p = tmp_path / "p.autocut.json"
    plan.save(p)
    r = guard_uncertain(p, 0.75)
    assert r["restored"] == 2 and r["seconds"] == 3.0
    again = Plan.load(p)
    acts = {s.id: s.action for s in again.output().segments}
    assert acts == {"d1": "keep", "d2": "keep", "d3": "cut", "d4": "cut", "d5": "keep"}  # 高置信和锁定的不动
    assert any("保守策略" in n for n in again.notes) and again.revision == 2
    assert guard_uncertain(p, 0.75)["restored"] == 0  # 幂等


# ------------------------------------------------------------------ commentary（假 LLM / 假 TTS，真 cut / render）


class _FakeGW:
    """按 purpose 返回固定内容；记录调用，验证流水线给模型的是画面描述 + 原声，而不是别的。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def chat_json(self, model, messages, *, purpose, validate=None, **kw):
        self.calls.append((purpose, messages))
        if purpose == "vision":
            n = sum(1 for p in messages[0]["content"] if p.get("type") == "image_url")
            out = {"frames": [{"i": i + 1, "desc": f"画面{i + 1}：角色在移动"} for i in range(n)]}
        elif purpose == "script":
            out = {"title": "测试标题", "lines": [{"at": 1.0, "text": "开场先看这一手"}, {"at": 8.0, "text": "这里对面开团了"}, {"at": 16.0, "text": "漂亮的收尾"}]}
        else:
            out = {"lines": []}
        if validate:
            validate(out)
        return out

    def spent_cny(self) -> float:
        return 0.123


class _FakeTTS:
    stats = {"hit": 0, "miss": 3}

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp

    def synth_many(self, lines, *, voice, lang):
        from test_worker_commentary import _tone_wav  # noqa: PLC0415

        res = []
        for i, t in enumerate(lines):
            p = _tone_wav(self.tmp / f"tts{abs(hash(t))}.wav", 1.2)
            res.append((p, 1.2, "mini"))
        return res


def test_commentary_pipeline_end_to_end_with_fakes(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_commentary
    from autocut.worker.pipeline import start

    job = app.store.create_job("game", {"budget_cny": 2.0})["id"]
    shutil.copy(media / "game_commentary.mp4", app.store.job_dir(job) / "src" / "game.mp4")
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/game.mp4"})
    gw, tts = _FakeGW(), _FakeTTS(tmp_path)
    run = start(app, job, "commentary", {"game": "测试游戏"}, lambda ctx: pipe_commentary.run_commentary(ctx, gateway=gw, tts=tts))
    r = wait(app, job, run["id"])
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    d = app.store.job_dir(job)
    dv = res["deliverables"][0]
    assert (d / dv["final"]).stat().st_size > 10_000 and (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES
    assert [s["name"] for s in r["stages"]][:5] == ["cut", "vision", "script", "tts", "mix"]
    assert "3 句解说" in res["summary"] and "¥0.123" in res["summary"] and res["title"] == "测试标题"
    plan = json.loads((d / "work" / "game.commentary.autocut.json").read_text())
    bed = plan["outputs"][0]["beds"][0]
    assert bed["role"] == "narration" and bed["duck_main_db"] == 14.0 and plan["outputs"][0]["captions"]["burn"] is True
    # 模型收到了画面描述，没收到别的
    assert any(p == "script" and "画面1" in m[0]["content"] for p, m in gw.calls)
    # 成片音频里解说确实混进去了：有 srt 字幕
    assert (d / "out" / "game.commentary.srt").exists() or any(x.endswith(".srt") for x in dv["extras"])


# ------------------------------------------------------------------ clips（假 LLM，真 ASR / 切点 / render）


class _ClipsGW:
    """提名：把文字稿每 3 句分一组；评审：第 2 个候选故意给 standalone=2（应被淘汰）；QA 全过。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.seen_review_text: list[str] = []
        self.nominate_prompt = ""

    def chat_json(self, model, messages, *, purpose, validate=None, **kw):
        import re  # noqa: PLC0415

        self.calls.append(purpose)
        if purpose == "nominate":
            text = messages[0]["content"]
            self.nominate_prompt = text
            ids = re.findall(r"^\[(s\d{4}) ", text, re.M)
            out = {"candidates": [{"first": ids[i], "last": ids[min(i + 2, len(ids) - 1)], "title": f"片段{i // 3 + 1}", "hook": "h", "reason": f"理由{i // 3 + 1}", "confidence": 0.8}
                                  for i in range(0, len(ids) - 2, 3)]}
        elif purpose == "review":
            n = self.calls.count("review")
            self.seen_review_text.append(messages[0]["content"])
            out = {"hook": 4, "standalone": 2 if n == 2 else 4, "arc": 4, "density": 4, "safety": 5, "fit": 4, "problems": ["开头悬空指代"] if n == 2 else []}
        elif purpose == "qa":
            out = {"black": False, "frozen": False, "caption_overflow": False, "subject_cropped": False, "note": "ok"}
        else:
            out = {}
        if validate:
            validate(out)
        return out

    def spent_cny(self) -> float:
        return 0.2


def test_clips_pipeline_funnel_and_review_gate(app: App, media: Path, tmp_path: Path) -> None:
    import subprocess  # noqa: PLC0415

    from autocut.worker import pipe_clips  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("clips", {"purpose": "找讲得最清楚的片段", "budget_cny": 2.0, "attribution": {"repost_label": True}})["id"]
    d = app.store.job_dir(job)
    lst = tmp_path / "l.txt"
    lst.write_text("".join(f"file '{media / 'lecture_en.mp4'}'\n" for _ in range(4)))
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(d / "src" / "long.mp4")], check=True)
    ingest.register_source(app.store, job, {"kind": "url", "file": "src/long.mp4", "url": "https://example.com/long", "title": "长课", "channel": "某人"})
    gw = _ClipsGW()
    run = start(app, job, "clips", {"n": 2, "length": [8, 40], "extra": 1, "lang": "en"}, lambda c: pipe_clips.run_clips(c, gateway=gw))
    r = wait(app, job, run["id"], 900)
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    f = res["funnel"]
    assert f["nominated"] >= 3 and f["review_ok"] == f["hard_ok"] - 1 and f["delivered"] >= 1  # 被评审淘汰了恰好一条
    assert "宁缺毋滥" not in res["summary"]
    mains = [x for x in res["deliverables"] if x["role"] == "main"]
    assert 1 <= len(mains) <= 2
    for dv in res["deliverables"]:
        assert (d / dv["final"]).stat().st_size > 5_000 and (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES
        assert dv["range"][1] > dv["range"][0] and dv["title"]
    plan = json.loads((d / "work" / "long.clips.r1.autocut.json").read_text())
    assert all(o["captions"]["burn"] and o["width"] == 1080 and o["height"] == 1920 for o in plan["outputs"])
    # 独立评审只看到文字稿，看不到提名理由
    assert gw.seen_review_text and not any("理由" in t for t in gw.seen_review_text)
    assert "转载自 某人《长课》" in res["summary"]
    st = delivery.state(app.store, job)
    assert st["status"] == "awaiting_confirmation" and len(st["deliverables"]) == len(res["deliverables"])
    # 拒绝其中一条 → 禁区 + 补做，且被拒区间不会再被选
    victim = mains[0]
    delivery.reject(app.store, job, "boring", "没意思", [victim["id"]])
    rc = delivery.rejection_context(app.store, job)
    assert rc["zones"] == [tuple(victim["range"])]
    gw2 = _ClipsGW()
    run2 = start(app, job, "clips", {"n": 2, "length": [8, 40], "extra": 0, "lang": "en"}, lambda c: pipe_clips.run_clips(c, gateway=gw2))
    r2 = wait(app, job, run2["id"], 900)
    assert r2["status"] == "ok", r2.get("result")
    assert "没意思" in gw2.nominate_prompt and "boring" in gw2.nominate_prompt  # 反馈进了下一轮提示
    st2 = delivery.state(app.store, job)
    assert victim["id"] not in [x["id"] for x in delivery.live(st2)]
    new = [x for x in delivery.live(st2) if x["id"].startswith("r2_")]
    for x in new:  # 新选的段和被拒区间不重叠
        a, b = x["range"]
        assert not (a < victim["range"][1] and victim["range"][0] < b)


def test_clips_pipeline_stops_after_three_rejection_rounds(app: App) -> None:
    from autocut.worker import pipe_clips  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("clips", {})["id"]
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/x.mp4"})
    (app.store.job_dir(job) / "rejections.json").write_text(json.dumps({"items": [{"reason": "boring", "text": "", "outputs": [], "zones": []}] * 3}))
    run = start(app, job, "clips", {}, lambda c: pipe_clips.run_clips(c, gateway=_ClipsGW()))
    r = wait(app, job, run["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "too_many_rounds" and "更明确" in r["result"]["error"]["fix"]
