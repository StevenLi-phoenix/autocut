"""异步 run：排队 → 子进程 → 进度 → 取消。重活共用 heavy 槽位（默认 1），避免与烤肉争 CPU/编码器。"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autocut.worker.config import WorkerConfig
from autocut.worker.policy import PolicyError, check_command, is_heavy
from autocut.worker.store import Store

log = logging.getLogger(__name__)
PID_RE = re.compile(r"^pid=(\d+)")
TAIL_BYTES = 4000
DONE = {"ok", "failed", "cancelled"}


def build_env(cfg: WorkerConfig) -> dict[str, str]:
    """子进程环境：干净的 PATH（私有 ffmpeg 在最前）、固定编码器。不透传 worker 自己的环境变量。"""
    parts = [p for p in (cfg.ffmpeg_dir, "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin") if p]
    env = {
        "PATH": os.pathsep.join(parts),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "en_US.UTF-8",
        "AUTOCUT_ENCODER": cfg.encoder,
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    for k in ("AUTOCUT_WHISPER_MODEL", "AUTOCUT_WHISPER_CLI", "TMPDIR"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.update(cfg.extra_env)
    return env


class Runner:
    def __init__(self, cfg: WorkerConfig, store: Store) -> None:
        self.cfg, self.store = cfg, store
        self._heavy = threading.Semaphore(cfg.heavy_slots)
        self._cv = threading.Condition()
        self._procs: dict[tuple[str, str], subprocess.Popen[str]] = {}
        self._cancel: set[tuple[str, str]] = set()
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._recover()

    # ---- 公共接口
    def on_finish(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def submit(self, job_id: str, cmd: str, args: list[str], *, note: str = "") -> dict[str, Any]:
        job_dir = self.store.job_dir(job_id)
        self.store.get_job(job_id)
        argv = check_command(cmd, args, job_dir)
        full = [*self.cfg.autocut_cmd, "-v", cmd, *argv]
        return self._start(job_id, "autocut", cmd, argv, full, heavy=is_heavy(cmd), cwd=job_dir, env=build_env(self.cfg), note=note)

    def submit_raw(self, job_id: str, kind: str, label: str, full: list[str], *, heavy: bool, env: dict[str, str], note: str = "") -> dict[str, Any]:
        """给 ingest / pipeline 用：命令已由 worker 自己构造，不经白名单。"""
        job_dir = self.store.job_dir(job_id)
        self.store.get_job(job_id)
        return self._start(job_id, kind, label, [], full, heavy=heavy, cwd=job_dir, env=env, note=note)

    def get(self, job_id: str, run_id: str, wait: float = 0.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(wait, 120.0))
        with self._cv:
            while True:
                run = self._fresh(job_id, run_id)
                if run["status"] in DONE or time.monotonic() >= deadline:
                    return run
                self._cv.wait(timeout=min(1.0, max(0.05, deadline - time.monotonic())))

    def cancel(self, job_id: str, run_id: str) -> dict[str, Any]:
        key = (job_id, run_id)
        run = self._fresh(job_id, run_id)
        if run["status"] in DONE:
            return run
        self._cancel.add(key)
        proc = self._procs.get(key)
        log.info("取消 run %s/%s pid=%s", job_id, run_id, run.get("pid"))
        if proc and proc.poll() is None:
            self._terminate(proc, run.get("pid"))
        with self._cv:
            self._cv.notify_all()
        return self._fresh(job_id, run_id)

    def notify(self) -> None:
        with self._cv:
            self._cv.notify_all()

    # ---- 内部
    def _fresh(self, job_id: str, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(job_id, run_id)
        if run["status"] == "running":
            run["progress"] = self._progress(job_id, run_id)
        return run

    def _paths(self, job_id: str, run_id: str) -> tuple[Path, Path]:
        d = self.store.job_dir(job_id) / ".runs"
        return d / f"{run_id}.out", d / f"{run_id}.err"

    def _progress(self, job_id: str, run_id: str) -> dict[str, Any]:
        _, err = self._paths(job_id, run_id)
        try:
            with err.open("rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 2000))
                lines = [ln for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
        except FileNotFoundError:
            return {}
        return {"last": lines[-1][:300]} if lines else {}

    def _start(self, job_id: str, kind: str, label: str, args: list[str], full: list[str], *, heavy: bool, cwd: Path, env: dict[str, str], note: str) -> dict[str, Any]:
        run_id = self.store.new_run_id()
        run = {"id": run_id, "job": job_id, "kind": kind, "cmd": label, "args": args, "status": "queued", "heavy": heavy, "created_at": time.time(), "note": note}
        self.store.save_run(job_id, run)
        log.info("run 入队：%s/%s %s heavy=%s", job_id, run_id, label, heavy)
        threading.Thread(target=self._work, args=(job_id, run, full, cwd, env), name=f"run-{run_id}", daemon=True).start()
        return run

    def _terminate(self, proc: subprocess.Popen[str], pid: int | None) -> None:
        # autocut 自己处理 SIGTERM（停子 ffmpeg、删半成品）；先温和，5 秒后强杀
        target = proc.pid
        try:
            os.kill(target, signal.SIGTERM)
        except ProcessLookupError:
            return

        def reaper() -> None:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                log.warning("pid %d 8 秒未退出，SIGKILL 进程组", target)
                try:
                    os.killpg(target, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        threading.Thread(target=reaper, daemon=True).start()

    def _work(self, job_id: str, run: dict[str, Any], full: list[str], cwd: Path, env: dict[str, str]) -> None:
        key = (job_id, run["id"])
        out_p, err_p = self._paths(job_id, run["id"])
        got_slot = False
        try:
            if run["heavy"]:
                while not self._heavy.acquire(timeout=0.5):
                    if key in self._cancel:
                        raise _Cancelled
                got_slot = True
            if key in self._cancel:
                raise _Cancelled
            cmdline = ["/usr/bin/nice", "-n", str(self.cfg.nice), *full] if self.cfg.nice else full
            t0 = time.perf_counter()
            with out_p.open("wb") as fo, err_p.open("wb") as fe:
                proc = subprocess.Popen(cmdline, cwd=cwd, env=env, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, start_new_session=True)
                self._procs[key] = proc
                run.update(status="running", pid=proc.pid, started_at=time.time())
                self.store.save_run(job_id, run)
                log.info("run 开始：%s/%s pid=%d %s", job_id, run["id"], proc.pid, full[: len(full)][:6])
                rc = proc.wait()
            run["duration_s"] = round(time.perf_counter() - t0, 2)
            run["exit_code"] = rc
            result = _parse_json(out_p)
            run["result"] = result
            run["stderr_tail"] = _tail(err_p)
            if key in self._cancel or (rc != 0 and _is_cancel(result)):
                run["status"] = "cancelled"
            else:
                run["status"] = "ok" if rc == 0 and (result is None or result.get("ok", True)) else "failed"
        except _Cancelled:
            run.update(status="cancelled", stderr_tail="（排队中被取消）")
        except Exception as e:  # noqa: BLE001
            log.exception("run 异常：%s/%s", job_id, run["id"])
            run.update(status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            if got_slot:
                self._heavy.release()
            self._procs.pop(key, None)
            self._cancel.discard(key)
            run["finished_at"] = time.time()
            self.store.save_run(job_id, run)
            log.info("run 结束：%s/%s status=%s rc=%s %.1fs", job_id, run["id"], run["status"], run.get("exit_code"), run.get("duration_s", 0))
            with self._cv:
                self._cv.notify_all()
            for fn in self._listeners:
                try:
                    fn(job_id, run)
                except Exception:  # noqa: BLE001
                    log.exception("on_finish 监听器失败")

    def _recover(self) -> None:
        """worker 重启后，上次遗留的 queued/running 记录标为 failed（子进程随旧 worker 一起死或成孤儿，不假装还在跑）。"""
        if not self.cfg.root.exists():
            return
        for jd in self.cfg.root.iterdir():
            rd = jd / ".runs"
            if not rd.is_dir():
                continue
            for p in rd.glob("r-*.json"):
                try:
                    r = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                if r.get("status") in ("queued", "running"):
                    pid = r.get("pid")
                    alive = False
                    if pid:
                        try:
                            os.kill(pid, 0)
                            alive = True
                        except ProcessLookupError:
                            pass
                    r.update(status="failed", error="worker 重启，任务中断" + (f"（pid {pid} 可能仍在运行，需要时 kill {pid}）" if alive else ""), finished_at=time.time())
                    p.write_text(json.dumps(r, ensure_ascii=False, indent=2))
                    log.warning("恢复：%s 标为 failed", p)


class _Cancelled(Exception):
    pass


def _parse_json(p: Path) -> dict[str, Any] | None:
    try:
        text = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {"ok": False, "raw": text[-TAIL_BYTES:]}


def _tail(p: Path) -> str:
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return ""
    return data[-TAIL_BYTES:].decode("utf-8", "replace")


def _is_cancel(result: dict[str, Any] | None) -> bool:
    err = (result or {}).get("error") or {}
    return isinstance(err, dict) and err.get("code") == "cancelled"
