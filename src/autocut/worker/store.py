"""job 目录与 run 记录（文件即数据库，进程重启后可恢复）。

<root>/<job>/
  job.json            任务配置：目的、署名/转载策略、预算、创建时间
  provenance.json     素材来源登记（ingest 写入）
  src/                素材
  work/               方案、缓存、中间产物
  out/                成片与待确认文件
  .runs/<run>.json    run 记录；<run>.out / <run>.err 是 stdout / stderr
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from autocut.worker.config import RESERVED_NAMES
from autocut.worker.policy import PolicyError, check_job_id

log = logging.getLogger(__name__)
SUBDIRS = ("src", "work", "out", ".runs")


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def job_dir(self, job_id: str) -> Path:
        check_job_id(job_id)
        if job_id in RESERVED_NAMES:
            raise PolicyError("reserved_job", f"保留名：{job_id}", "换一个 job 名")
        return self.root / job_id

    def new_job_id(self, name: str | None = None) -> str:
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in (name or "job"))[:32].strip("-_") or "job"
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"

    def create_job(self, name: str | None, config: dict[str, Any]) -> dict[str, Any]:
        job_id = self.new_job_id(name)
        d = self.job_dir(job_id)
        for sub in SUBDIRS:
            (d / sub).mkdir(parents=True, exist_ok=True)
        job = {"id": job_id, "created_at": time.time(), "config": config}
        atomic_write_json(d / "job.json", job)
        log.info("job 创建：%s config=%s", job_id, config)
        return job

    def get_job(self, job_id: str) -> dict[str, Any]:
        d = self.job_dir(job_id)
        job = read_json(d / "job.json")
        if job is None:
            raise PolicyError("no_such_job", f"没有这个 job：{job_id}", "先 POST /v1/jobs 创建，或 GET /v1/jobs 列出已有的")
        return job

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        out = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir(), reverse=True):
            if d.name in RESERVED_NAMES or not (d / "job.json").exists():
                continue
            j = read_json(d / "job.json", {})
            out.append({"id": j.get("id", d.name), "created_at": j.get("created_at"), "config": j.get("config", {})})
            if len(out) >= limit:
                break
        return out

    def new_run_id(self) -> str:
        return f"r-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}"

    def run_path(self, job_id: str, run_id: str) -> Path:
        return self.job_dir(job_id) / ".runs" / f"{run_id}.json"

    def save_run(self, job_id: str, run: dict[str, Any]) -> None:
        atomic_write_json(self.run_path(job_id, run["id"]), run)

    def get_run(self, job_id: str, run_id: str) -> dict[str, Any]:
        r = read_json(self.run_path(job_id, run_id))
        if r is None:
            raise PolicyError("no_such_run", f"没有这个 run：{run_id}", f"GET /v1/jobs/{job_id} 看该 job 的 runs")
        return r

    def list_runs(self, job_id: str) -> list[dict[str, Any]]:
        d = self.job_dir(job_id) / ".runs"
        runs = [read_json(p, {}) for p in sorted(d.glob("r-*.json"))] if d.exists() else []
        return runs
