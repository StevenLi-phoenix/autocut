"""worker 流水线集成测试：真 autocut + 合成素材（fixtures/media，缺失时 skip）。"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

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
