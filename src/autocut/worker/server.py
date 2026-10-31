"""HTTP 外壳（stdlib，仅 127.0.0.1）。路由见 docs/dev/worker.md。

所有响应都是 JSON：成功 {"ok": true, ...}；失败 {"ok": false, "error": {code, message, fix}}，状态码 4xx/5xx。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from autocut.worker import delivery, ingest, policy, signals
from autocut.worker.config import WorkerConfig
from autocut.worker.policy import PolicyError
from autocut.worker.runner import Runner
from autocut.worker.store import Store

log = logging.getLogger(__name__)
MAX_BODY = 1_000_000
MAX_UPLOAD = 20 * 1024**3
SAFE_NAME = re.compile(r"[^A-Za-z0-9._一-鿿-]+")
URL_RE = re.compile(r"^https?://[^\s]+$")


class App:
    """路由与业务；与 http 层分开，方便单测。"""

    def __init__(self, cfg: WorkerConfig, runner: Runner | None = None, store: Store | None = None) -> None:
        self.cfg = cfg
        self.store = store or Store(cfg.root)
        self.runner = runner or Runner(cfg, self.store)
        self.runner.on_finish(self._after_run)
        self.pipelines: dict[str, Any] = {}  # name -> fn(app, job_id, opts) -> run
        self.pipeline_ctx: dict[tuple[str, str], Any] = {}
        self.started = time.time()

    # ---- 事件钩子：ingest 完成后登记来源
    def _after_run(self, job_id: str, run: dict[str, Any]) -> None:
        if run.get("kind") != "ingest" or run.get("status") != "ok":
            return
        res = run.get("result") or {}
        if not res.get("ok"):
            return
        d = self.store.job_dir(job_id)
        vp = Path(res["video_path"])
        rel = os.path.relpath(vp, d)
        entry = ingest.register_source(self.store, job_id, {
            "kind": "url", "file": rel, "url": run["args"][0], "title": res.get("title"), "channel": res.get("channel"),
            "video_id": res.get("video_id"), "description": res.get("description"), "live_status": res.get("live_status"),
            "subtitle_file": os.path.relpath(res["subtitle_path"], d) if res.get("subtitle_path") else None, **ingest.probe(vp)})
        run["registered"] = entry["id"]
        self.store.save_run(job_id, run)

    # ---- 路由
    def handle(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = [p for p in path.split("/") if p]
        q = lambda k, d=None: (query.get(k) or [d])[0]  # noqa: E731
        if parts == ["health"] or parts == ["v1", "health"]:
            return 200, {"ok": True, "uptime_s": round(time.time() - self.started), "root": str(self.cfg.root), "encoder": self.cfg.encoder,
                         "root_ok": self.cfg.root.is_dir(), "commands": sorted(policy.COMMANDS)}
        if parts == ["v1", "metrics"]:
            return 200, {"ok": True, "pass_rate": delivery.pass_rate(self.cfg.root)}
        if parts[:2] != ["v1", "jobs"]:
            raise PolicyError("not_found", f"没有这个路径：{path}", "GET /v1/health 看服务是否正常；路由见 docs/dev/worker.md")
        rest = parts[2:]
        if not rest:
            if method == "POST":
                cfgd = body.get("config") or {}
                if not isinstance(cfgd, dict):
                    raise PolicyError("bad_config", "config 必须是对象", '例如 {"purpose": "...", "attribution": {"repost_label": true}}')
                return 200, {"ok": True, "job": self.store.create_job(body.get("name"), cfgd)}
            return 200, {"ok": True, "jobs": self.store.list_jobs(int(q("limit", "30")))}
        job_id = rest[0]
        job = self.store.get_job(job_id)
        d = self.store.job_dir(job_id)
        sub = rest[1:]
        if not sub:
            return 200, {"ok": True, "job": job, "provenance": ingest.read_json(d / "provenance.json", {"sources": []}),
                         "state": delivery.state(self.store, job_id).get("status"), "rejections": delivery.rejection_context(self.store, job_id), "files": self._files(d), "runs": [self._run_brief(r) for r in self.store.list_runs(job_id)[-20:]]}
        if sub == ["ingest"] and method == "POST":
            return 200, {"ok": True, "run": self._ingest(job_id, body)}
        if sub == ["run"] and method == "POST":
            run = self.runner.submit(job_id, str(body.get("cmd", "")), list(body.get("args") or []), note=str(body.get("note", "")))
            return 200, {"ok": True, "run": self._run_brief(run), "next": [{"cmd": f"GET /v1/jobs/{job_id}/runs/{run['id']}?wait=30", "why": "等待结果"}]}
        if sub and sub[0] == "runs" and len(sub) >= 2:
            run_id = sub[1]
            if len(sub) == 3 and sub[2] == "cancel" and method == "POST":
                from autocut.worker import pipeline

                if pipeline.cancel(self, job_id, run_id):
                    self.runner.get(job_id, run_id, wait=15)
                    return 200, {"ok": True, "run": self._run_brief(self.store.get_run(job_id, run_id))}
                return 200, {"ok": True, "run": self._run_brief(self.runner.cancel(job_id, run_id))}
            if len(sub) == 2:
                return 200, {"ok": True, "run": self.runner.get(job_id, run_id, float(q("wait", "0")))}
        if sub == ["files"]:
            return 200, {"ok": True, "files": self._files(d, q("dir", ""))}
        if sub == ["provenance"]:
            return 200, {"ok": True, "provenance": ingest.read_json(d / "provenance.json", {"sources": []}), "credits": ingest.credits_text(self.store, job_id)}
        if sub and sub[0] == "pipelines" and len(sub) == 2 and method == "POST":
            fn = self.pipelines.get(sub[1])
            if not fn:
                raise PolicyError("no_such_pipeline", f"没有这个流水线：{sub[1]}", f"可用：{'、'.join(sorted(self.pipelines)) or '（暂无）'}")
            return 200, {"ok": True, "run": fn(self, job_id, body)}
        if sub == ["frame"] and method == "POST":  # 按时间点取一帧（agent 用 read_image 看）
            src = policy.inside(d, str(body.get("source", "")))
            t = float(body.get("t", 0))
            if t < 0:
                raise PolicyError("bad_time", "t 不能为负", "秒数")
            w = max(160, min(int(body.get("width", 960)), 1920))
            dst = d / "work" / "frames-req" / f"{src.stem}-{int(t * 10):07d}.jpg"
            dst.parent.mkdir(parents=True, exist_ok=True)
            signals.grab(src, t, dst, width=w)
            return 200, {"ok": True, "path": str(dst.relative_to(d)), "next": [{"cmd": f"autocut-remote fetch {job_id} {dst.relative_to(d)}", "why": "拉到本机后用 read_image 看"}]}
        if sub == ["preview"] and method == "POST":  # 重新压预览（微信上传超时时用更小的）
            mib = float(body.get("max_mib", 4))
            st = delivery.state(self.store, job_id)
            out = []
            for dv in delivery.live(st):
                final = policy.inside(d, dv["final"])
                dst = d / "out" / f"preview-{int(mib * 10)}-{final.stem}.mp4"
                prev = delivery.make_preview(final, dst, max_bytes=int(mib * 1024 * 1024))
                dv["preview"] = str(dst.relative_to(d))
                out.append({"id": dv["id"], "preview": dv["preview"], "bytes": prev["bytes"], "video_kbps": prev["video_kbps"]})
            delivery._save(self.store, job_id, st, "preview_resized", max_mib=mib)
            return 200, {"ok": True, "previews": out}
        if sub == ["state"]:
            return 200, {"ok": True, "state": delivery.state(self.store, job_id)}
        if sub == ["confirm"] and method == "POST":
            return 200, {"ok": True, "state": delivery.confirm(self.store, job_id, str(body.get("quote", "")))}
        if sub == ["reject"] and method == "POST":
            return 200, {"ok": True, "state": delivery.reject(self.store, job_id, str(body.get("reason", "")), str(body.get("text", "")), list(body.get("outputs") or []))}
        if sub == ["handoff"] and method == "POST":
            return 200, delivery.handoff(self.store, job_id, str(body.get("to", "local")), str(body.get("title", "")), str(body.get("desc", "")), self.cfg.root,
                                         kaorou={k: body.get(k) for k in ("tags", "tid", "visibility", "season", "api") if body.get(k) is not None})
        raise PolicyError("not_found", f"没有这个路径：{path}", "路由见 docs/dev/worker.md")

    def _run_brief(self, r: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "kind", "cmd", "args", "status", "pid", "heavy", "exit_code", "duration_s", "created_at", "started_at", "finished_at", "note", "registered", "error", "progress")
        return {k: r[k] for k in keys if k in r}

    def _files(self, d: Path, sub: str | None = "") -> list[dict[str, Any]]:
        base = policy.inside(d, sub or ".")
        out = []
        for p in sorted(base.rglob("*")) if base.is_dir() else []:
            rel = p.relative_to(d)
            if p.is_file() and not any(part.startswith(".") for part in rel.parts) and p.parent != d and not (rel.parts[0] == "work" and len(rel.parts) > 2):  # work/ 下的帧、LLM 原始响应等子目录不列
                out.append({"path": str(rel), "bytes": p.stat().st_size})
            if len(out) >= 300:
                break
        return out

    def _ingest(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        url, inbox = body.get("url"), body.get("inbox")
        if url:
            if not URL_RE.match(str(url)):
                raise PolicyError("bad_url", f"不是 http(s) 链接：{url}", "给完整链接，例如 https://www.youtube.com/watch?v=...")
            if not self.cfg.ingest_python:
                raise PolicyError("ingest_unavailable", "worker 没配置下载环境", "设置 AUTOCUT_WORKER_INGEST_PYTHON 指向烤肉 venv 的 python")
            d = self.store.job_dir(job_id)
            from autocut.worker import ingest_ytdlp

            meta = d / "work" / f"ingest-{int(time.time())}.json"
            full = [self.cfg.ingest_python, str(Path(ingest_ytdlp.__file__)), "--url", str(url), "--out", str(d / "src"), "--meta", str(meta)]
            run = self.runner.submit_raw(job_id, "ingest", "ingest", full, heavy=False, env=ingest.ingest_env(self.cfg), note=str(body.get("note", "")), args=[str(url)])
            return self._run_brief(run)
        if body.get("inbox_link"):
            e = ingest.link_inbox(self.cfg, self.store, job_id, str(body["inbox_link"]))
            return {"id": None, "status": "ok", "kind": "ingest", "registered": e["id"], "source": e}
        if inbox:
            e = ingest.from_inbox(self.cfg, self.store, job_id, str(inbox))
            return {"id": None, "status": "ok", "kind": "ingest", "registered": e["id"], "source": e}
        raise PolicyError("bad_ingest", "需要 url 或 inbox", '{"url": "https://..."} 或 {"inbox": "文件名"}；本地文件也可以 PUT /v1/jobs/<job>/upload?name=x.mp4')


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "autocut-worker/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("http %s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _dispatch(self) -> None:
        t0 = time.perf_counter()
        u = urlparse(self.path)
        code = 500
        try:
            query = parse_qs(u.query)
            parts = [p for p in u.path.split("/") if p]
            if self.command in ("GET", "HEAD") and len(parts) >= 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "file":
                code = self._file(parts[2], (query.get("path") or [""])[0])
                return
            if self.command == "PUT" and len(parts) == 4 and parts[3] == "upload":
                code, obj = 200, self._upload(parts[2], (query.get("name") or [""])[0])
                self._send(code, obj)
                return
            body: dict[str, Any] = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                if n > MAX_BODY:
                    raise PolicyError("body_too_large", "请求体太大", "大文件用 PUT .../upload")
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    raise PolicyError("bad_json", "请求体不是合法 JSON", "") from None
                if not isinstance(body, dict):
                    raise PolicyError("bad_json", "请求体必须是 JSON 对象", "")
            code, obj = self.app.handle(self.command, u.path, query, body)
        except PolicyError as e:
            code = 404 if e.code.startswith(("no_such", "not_found")) else 400
            obj = {"ok": False, "error": {"code": e.code, "message": e.message, "fix": e.fix}}
        except Exception as e:  # noqa: BLE001
            log.exception("请求失败 %s %s", self.command, self.path)
            code, obj = 500, {"ok": False, "error": {"code": "internal", "message": f"{type(e).__name__}: {e}", "fix": "看 worker 日志"}}
        finally:
            log.info("%s %s -> %s %.0fms", self.command, u.path, code, (time.perf_counter() - t0) * 1000)
        self._send(code, obj)

    def _upload(self, job_id: str, name: str) -> dict[str, Any]:
        store = self.app.store
        store.get_job(job_id)
        name = SAFE_NAME.sub("_", os.path.basename(name or "")).strip("._")
        if not name or Path(name).suffix.lower() not in ingest.MEDIA_EXT:
            raise PolicyError("bad_name", "name 缺失或扩展名不支持", "例如 ?name=talk.mp4")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_UPLOAD:
            raise PolicyError("bad_length", "需要 Content-Length（≤20GB）", "curl -T file.mp4 'http://127.0.0.1:8102/v1/jobs/<job>/upload?name=file.mp4'")
        dst = store.job_dir(job_id) / "src" / name
        tmp = dst.with_name(f".{name}.part")
        left = n
        with tmp.open("wb") as f:
            while left:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            tmp.unlink(missing_ok=True)
            raise PolicyError("upload_truncated", f"上传中断，少 {left} 字节", "重试")
        os.replace(tmp, dst)
        entry = ingest.register_source(store, job_id, {"kind": "upload", "file": f"src/{name}", **ingest.probe(dst)})
        return {"ok": True, "source": entry}

    def _file(self, job_id: str, rel: str) -> int:
        d = self.app.store.job_dir(job_id)
        self.app.store.get_job(job_id)
        p = policy.inside(d, rel)
        if not p.is_file() or any(part.startswith(".") for part in p.relative_to(d.resolve()).parts):
            raise PolicyError("no_such_file", f"没有这个文件：{rel}", "GET /v1/jobs/<job>/files 列出文件")
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)$", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            code = 206
        end = min(end, size - 1)
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command != "HEAD":
            with p.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left:
                    buf = f.read(min(1 << 20, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        return code

    do_GET = do_POST = do_PUT = do_HEAD = _dispatch


def make_server(cfg: WorkerConfig, app: App | None = None) -> ThreadingHTTPServer:
    app = app or App(cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    return srv


def main() -> int:
    logging.basicConfig(level=os.environ.get("AUTOCUT_WORKER_LOG", "INFO"), stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = WorkerConfig.from_env()
    os.environ["AUTOCUT_CACHE"] = str(cfg.root / "_worker" / "cache")  # 分析缓存不写到素材旁边（inbox 是只读的）
    policy.EXTRA_READ_ROOTS.append(cfg.root / "inbox")
    if cfg.host not in ("127.0.0.1", "::1", "localhost"):
        log.error("worker 只允许绑定回环地址，拒绝 %s", cfg.host)
        return 2
    if not cfg.root.is_dir():
        log.error("根目录不存在或不可读：%s（外置盘没挂载，或进程没有可移除宗卷授权？）", cfg.root)
        return 3
    from autocut.worker import pipelines  # noqa: PLC0415 —— 注册流水线（阶段 3+）

    app = App(cfg)
    pipelines.register(app)
    from autocut.worker.events import Emitter  # noqa: PLC0415

    emitter = Emitter(cfg, app.store)
    app.runner.on_finish(emitter)
    log.info("DSH 事件：%s", "已启用 → " + emitter.url if emitter.enabled else "未启用（secrets 里没有 AUTOCUT_DSH_EVENT_TOKEN）")
    srv = make_server(cfg, app)
    log.info("autocut-worker 监听 http://%s:%d root=%s encoder=%s heavy_slots=%d autocut=%s", cfg.host, cfg.port, cfg.root, cfg.encoder, cfg.heavy_slots, list(cfg.autocut_cmd))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
