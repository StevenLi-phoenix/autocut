"""流水线基础设施：一个 pipeline run = worker 进程里的一个线程，按阶段串起 autocut 子命令、ffmpeg 和（后续阶段的）LLM 调用。

run 记录和普通 run 同一格式（kind="pipeline"），多了 stages / result；agent 用同一套 wait / cancel。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autocut.worker import policy
from autocut.worker.runner import DONE

if TYPE_CHECKING:
    from autocut.worker.server import App

log = logging.getLogger(__name__)


class PipelineError(Exception):
    def __init__(self, code: str, message: str, fix: str = "", stage: str = "", details: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.fix, self.stage, self.details = code, message, fix, stage, details


class PipelineCancelled(Exception):
    pass


class Ctx:
    """流水线里能用的东西。所有路径都限定在 job 目录内。"""

    def __init__(self, app: App, job_id: str, run: dict[str, Any], opts: dict[str, Any]) -> None:
        self.app, self.job_id, self.run, self.opts = app, job_id, run, opts
        self.dir = app.store.job_dir(job_id).resolve()
        self.cancelled = threading.Event()
        self.current_sub: str | None = None
        self.t0 = time.perf_counter()

    # ---- 记录
    def stage(self, name: str, detail: str = "") -> None:
        if self.cancelled.is_set():
            raise PipelineCancelled
        stages = self.run.setdefault("stages", [])
        if stages and stages[-1].get("t_end") is None:
            stages[-1]["t_end"] = round(time.perf_counter() - self.t0, 1)
        stages.append({"name": name, "detail": detail, "t_start": round(time.perf_counter() - self.t0, 1), "t_end": None})
        self.run["progress"] = {"last": f"{name} {detail}".strip()}
        self.app.store.save_run(self.job_id, self.run)
        log.info("pipeline %s/%s 阶段：%s %s", self.job_id, self.run["id"], name, detail)

    def path(self, rel: str) -> Path:
        return policy.inside(self.dir, rel)

    # ---- 子命令
    def autocut(self, cmd: str, args: list[str], *, note: str = "", allow_fail: bool = False) -> dict[str, Any]:
        """跑一个 autocut 子命令并等它结束；失败抛 PipelineError（带 autocut 的 error.fix），取消抛 PipelineCancelled。"""
        stage = (self.run.get("stages") or [{}])[-1].get("name", "")
        sub = self.app.runner.submit(self.job_id, cmd, args, note=note or f"pipeline {self.run['id']}")
        self.current_sub = sub["id"]
        try:
            while True:
                if self.cancelled.is_set():
                    self.app.runner.cancel(self.job_id, sub["id"])
                    self.app.runner.get(self.job_id, sub["id"], wait=10)
                    raise PipelineCancelled
                r = self.app.runner.get(self.job_id, sub["id"], wait=2)
                last = (r.get("progress") or {}).get("last")
                if last and r["status"] == "running":
                    self.run["progress"] = {"last": f"{stage}：{last}"[:300]}
                if r["status"] in DONE:
                    break
        finally:
            self.current_sub = None
        if r["status"] == "cancelled":
            raise PipelineCancelled
        res = r.get("result") or {}
        if r["status"] != "ok" and not allow_fail:
            err = res.get("error") or {}
            raise PipelineError(err.get("code", "autocut_failed"), f"autocut {cmd} 失败：{err.get('message') or r.get('stderr_tail', '')[-300:]}", err.get("fix", ""), stage)
        res["_run"] = sub["id"]
        return res


def start(app: App, job_id: str, name: str, opts: dict[str, Any], fn: Callable[[Ctx], dict[str, Any]]) -> dict[str, Any]:
    app.store.get_job(job_id)
    run_id = app.store.new_run_id()
    run = {"id": run_id, "job": job_id, "kind": "pipeline", "cmd": name, "args": [], "opts": opts, "status": "running", "heavy": False,
           "created_at": time.time(), "started_at": time.time(), "stages": []}
    app.store.save_run(job_id, run)
    ctx = Ctx(app, job_id, run, opts)
    app.pipeline_ctx[(job_id, run_id)] = ctx
    log.info("pipeline 开始：%s/%s %s opts=%s", job_id, run_id, name, opts)

    def body() -> None:
        try:
            run["result"] = fn(ctx)
            run["status"] = "ok"
        except PipelineCancelled:
            run["status"] = "cancelled"
        except PipelineError as e:
            err = {"code": e.code, "message": e.message, "fix": e.fix, "stage": e.stage}
            if e.details is not None:
                err["details"] = e.details
            run.update(status="failed", result={"ok": False, "error": err})
        except Exception as e:  # noqa: BLE001
            from autocut.worker.llm import LLMError  # noqa: PLC0415

            stage = (run.get("stages") or [{}])[-1].get("name", "")
            if isinstance(e, LLMError):
                run.update(status="failed", result={"ok": False, "error": {"code": e.code, "message": e.message, "fix": e.fix, "stage": stage}})
            elif isinstance(e, policy.PolicyError):
                run.update(status="failed", result={"ok": False, "error": {"code": e.code, "message": e.message, "fix": e.fix}})
            else:
                log.error("pipeline 异常 %s/%s\n%s", job_id, run_id, traceback.format_exc())
                run.update(status="failed", result={"ok": False, "error": {"code": "pipeline_crashed", "message": f"{type(e).__name__}: {e}", "fix": "看 worker 日志", "stage": stage}})
        finally:
            if run["stages"] and run["stages"][-1].get("t_end") is None:
                run["stages"][-1]["t_end"] = round(time.perf_counter() - ctx.t0, 1)
            run["finished_at"] = time.time()
            run["duration_s"] = round(time.perf_counter() - ctx.t0, 1)
            app.store.save_run(job_id, run)
            app.pipeline_ctx.pop((job_id, run_id), None)
            log.info("pipeline 结束：%s/%s %s %.1fs", job_id, run_id, run["status"], run["duration_s"])
            app.runner.notify()
            for fn_ in app.runner._listeners:
                try:
                    fn_(job_id, run)
                except Exception:  # noqa: BLE001
                    log.exception("on_finish 监听器失败")

    threading.Thread(target=body, name=f"pipe-{run_id}", daemon=True).start()
    return run


def cancel(app: App, job_id: str, run_id: str) -> bool:
    ctx = app.pipeline_ctx.get((job_id, run_id))
    if not ctx:
        return False
    ctx.cancelled.set()
    if ctx.current_sub:
        app.runner.cancel(job_id, ctx.current_sub)
    return True
