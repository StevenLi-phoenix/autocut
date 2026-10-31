"""DSH 事件：流水线结束时 POST 到 DSH 的 /hooks/lsy（source=autocut）：autocut.needs_agent（准备好了，等 agent 做判断）、autocut.ready（成片等用户确认）、autocut.failed。

事件文字只描述"发生了什么 + 去哪看"，不含任何指令；agent 回 worker 取摘要（status / run）。失败不影响流水线本身（只记日志）。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from autocut.worker.config import WorkerConfig, load_secrets

log = logging.getLogger(__name__)
DEFAULT_URL = "http://127.0.0.1:3081/hooks/lsy"


def event_for(job_id: str, run: dict[str, Any]) -> dict[str, Any] | None:
    """pipeline run 结束 → 事件体；其它 run（单个 autocut 子命令、ingest）不发。"""
    if run.get("kind") != "pipeline" or run.get("status") not in ("ok", "failed"):
        return None
    name = run.get("cmd", "")
    res = run.get("result") or {}
    key = f"{job_id}-{run['id']}"
    if run["status"] == "ok" and res.get("state") == "needs_agent":
        return {"kind": "autocut.needs_agent", "key": key, "subject": job_id, "title": f"autocut {name} 准备好了，等你做判断：{res.get('needs', '')}（job {job_id}）",
                "text": f"run {run['id']}；用 `autocut-remote wait {job_id} {run['id']}` 读 brief，按 autocut skill 做判断后提交对应的 *_finish 流水线"}
    if run["status"] == "ok" and res.get("state") == "awaiting_confirmation":
        n = len(res.get("deliverables") or [])
        return {"kind": "autocut.ready", "key": key, "subject": job_id, "title": f"autocut {name} 完成，{n} 条成片等用户确认（job {job_id}）",
                "text": f"run {run['id']}；摘要：{str(res.get('summary', ''))[:3000]}"}
    if run["status"] == "failed":
        err = res.get("error") or {}
        return {"kind": "autocut.failed", "key": key, "subject": job_id, "title": f"autocut {name} 失败：{str(err.get('code', ''))}（job {job_id}）",
                "text": f"run {run['id']}；{str(err.get('message', ''))[:600]}；fix：{str(err.get('fix', ''))[:300]}"}
    return None


class Emitter:
    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def __init__(self, cfg: WorkerConfig, jobs: Any = None) -> None:
        sec = load_secrets(cfg.secrets_file)
        self.token = sec.get("AUTOCUT_DSH_EVENT_TOKEN", "")
        self.url = sec.get("AUTOCUT_DSH_EVENT_URL") or DEFAULT_URL
        self.jobs = jobs  # Store：用来读 job.config.notify

    def __call__(self, job_id: str, run: dict[str, Any]) -> None:
        ev = event_for(job_id, run)
        if ev is None:
            return
        if self.jobs is not None:
            try:
                if (self.jobs.get_job(job_id).get("config") or {}).get("notify") is False:
                    log.info("事件 %s 未发送：job 配置了 notify=false（测试/批处理 job 不打扰用户）", ev["kind"])
                    return
            except Exception:  # noqa: BLE001
                pass
        if not self.enabled:
            log.info("事件 %s 未发送：没有配置 AUTOCUT_DSH_EVENT_TOKEN", ev["kind"])
            return
        req = urllib.request.Request(self.url, data=json.dumps(ev, ensure_ascii=False).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read()[:200]
            log.info("事件已发送 %s key=%s → %s %s", ev["kind"], ev["key"], r.status, body)
        except (urllib.error.URLError, OSError) as e:
            log.warning("事件发送失败 %s：%s", ev["kind"], e)
