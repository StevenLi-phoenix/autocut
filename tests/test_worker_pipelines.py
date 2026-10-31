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
    assert [s["name"] for s in r["stages"]] == ["cut", "check-plan", "render", "preview"]
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
