"""审阅页的本地服务器：只用标准库，只绑定 127.0.0.1。

页面（static/）通过下面这些接口读写同一份方案 JSON，agent 也在改这份文件，所以：
- 读：每次从磁盘读（带 stat 缓存），不在内存里藏一份“真相”；
- 写：PUT 必须带上次读到的 revision（和内容哈希 etag），对不上就返回 409 并附上磁盘上的版本，绝不静默覆盖；
- 出片/体检/导出：调用别的模块，import 失败返回 503，不让整个服务崩掉。

接口：
  GET  /api/plan                 方案 + 摘要 + 每个素材的元数据和 URL
  PUT  /api/plan                 {"revision": N, "etag"?: "...", "plan": {...}}；也接受直接带 revision 的整份方案
  GET  /api/plan/status          {revision, etag}，页面轮询它发现外部修改
  GET  /media/<source_id>        素材文件，支持 Range
  GET  /api/words/<source_id>    逐词文字稿（只读缓存，不触发识别）
  GET  /api/peaks/<source_id>?per_sec=50   波形峰值
  POST /api/render               {"output_ids"?: [...]} → {"job": id}
  GET  /api/jobs/<id>            进度/结果；POST /api/jobs/<id>/cancel 取消
  POST /api/check                {"output_id"?: ...} → 体检报告
  POST /api/export               {"format": "fcpxml|xmeml|edl|srt|markers", "output_id"?: ...}
  POST /api/relocate             {"source": id, "path": "...", "revision": N}：重新定位丢失的素材
  POST /api/reveal               {"path": "..."}：在 Finder 里显示本次生成的文件
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import json
import logging
import mimetypes
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from autocut.plan import Plan, PlanError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
EXPORT_FORMATS = ("fcpxml", "xmeml", "edl", "srt", "markers")
_LOOPBACK = {"127.0.0.1", "localhost"}
_MAX_BODY = 64 * 1024 * 1024
_CHUNK = 256 * 1024
_STATIC_SUFFIXES = {".html", ".js", ".css", ".svg", ".png", ".ico", ".json", ".woff2"}
_PATH_KEYS = {"path", "paths", "srt_path", "output", "outputs", "file", "files", "out", "out_path"}
_MODULE_LABEL = {"autocut.render": "出片", "autocut.check": "体检", "autocut.export": "导出"}


class ApiError(Exception):
    """带 HTTP 状态码的错误；消息直接显示给用户，所以要说清楚发生了什么、怎么办。"""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ---------- 小工具 ----------


def _jsonify(x: Any) -> Any:
    """把出片/体检/导出模块返回的各种对象转成能 JSON 序列化的结构。"""
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    if isinstance(x, Path):
        return str(x)
    if isinstance(x, dict):
        return {str(k): _jsonify(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset)):
        return [_jsonify(v) for v in x]
    if hasattr(x, "to_dict") and callable(x.to_dict):
        return _jsonify(x.to_dict())
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return _jsonify(dataclasses.asdict(x))
    return str(x)


def _slim(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _slim(v) for k, v in x.items() if k not in _RESULT_DROP}
    if isinstance(x, list):
        return [_slim(v) for v in x]
    return x


def _file_paths(x: Any, key: str | None = None) -> list[str]:
    """从结果里找出已经存在的文件路径（给“在 Finder 中显示”用）。"""
    found: list[str] = []
    if isinstance(x, str):
        if key in _PATH_KEYS or key is None:
            try:
                if Path(x).is_absolute() and Path(x).exists():
                    found.append(x)
            except (OSError, ValueError):
                pass
    elif isinstance(x, dict):
        for k, v in x.items():
            found += _file_paths(v, k)
    elif isinstance(x, list):
        for v in x:
            found += _file_paths(v, key)
    return found


def _call_flex(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """只传 fn 声明过的关键字参数：别的模块的签名还在演进，多传一个参数不该让整个功能挂掉。"""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def _parse_progress(args: tuple[Any, ...], kw: dict[str, Any]) -> tuple[str | None, float | None]:
    """进度回调的参数形状不固定：(stage, fraction) / (fraction,) / (dict,) / (对象,) / 关键字。都认。"""
    stage: str | None = None
    frac: float | None = None
    items: list[Any] = list(args)
    if kw:
        items.append(kw)
    for a in items:
        if isinstance(a, bool):
            continue
        if isinstance(a, (int, float)):
            frac = float(a)
        elif isinstance(a, str):
            stage = a
        elif isinstance(a, dict):
            stage = a.get("stage") or a.get("message") or a.get("name") or stage
            for k in ("fraction", "progress", "percent", "pct"):
                if isinstance(a.get(k), (int, float)):
                    frac = float(a[k])
                    if k in ("percent", "pct"):
                        frac /= 100
                    break
        else:
            stage = getattr(a, "stage", None) or getattr(a, "message", None) or stage
            for k in ("fraction", "progress", "percent"):
                if isinstance(getattr(a, k, None), (int, float)):
                    frac = float(getattr(a, k))
                    if k == "percent":
                        frac /= 100
                    break
    if frac is not None:
        frac = min(max(frac, 0.0), 1.0)
    return stage, frac


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析 Range 头 → (起, 止) 含两端；没有 Range 或看不懂（比如多段）返回 None（按整个文件处理）；不可满足抛 ApiError(416)。"""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    a, b = m.groups()
    if a == "":
        n = int(b)
        if n == 0 or size == 0:
            raise ApiError(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, f"Range 无法满足（文件 {size} 字节）", size=size)
        return max(0, size - n), size - 1
    start = int(a)
    end = min(int(b), size - 1) if b else size - 1
    if start >= size or start > end:
        raise ApiError(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, f"Range 起点 {start} 超出文件大小 {size}", size=size)
    return start, end


# ---------- 后台任务 ----------


class JobCancelled(InterruptedError):
    pass


# render.render 的 progress(stage, fraction) 里 fraction 是“当前这个输出”的 0–1，每个输出都会重来一遍。
# 这里把阶段映射成整个输出内的权重，再按输出个数折算成整体进度，进度条才不会来回跳。
_STAGES = {"prepare": ("准备", 0.0, 0.05), "measure": ("测响度", 0.05, 0.20), "encode": ("编码", 0.25, 0.70), "verify": ("校验成片", 0.95, 0.05), "done": ("完成", 1.0, 0.0)}
_RESULT_DROP = {"command", "filter_graph"}  # 出片结果里又长又只有排错才用的字段，不发给页面


class Job:
    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "running"  # running | done | error | cancelled
        self.stage = "准备中"
        self.fraction = 0.0
        self.started = time.time()
        self.finished: float | None = None
        self.result: Any = None
        self.error: str | None = None
        self.error_type: str | None = None
        self.cancel = threading.Event()
        self.index = 0  # 正在出第几个输出（1 起）
        self.total = 1
        self._last_stage = ""

    def update(self, stage: str | None, frac: float | None) -> None:
        """把 render 回调的 (stage, fraction) 折算成整体进度。"""
        known = _STAGES.get(stage or "")
        if known is None:
            if stage:
                self.stage = stage
            if frac is not None:
                self.fraction = max(self.fraction, min(frac, 0.99))
            return
        label, base, span = known
        if stage == "prepare" and self._last_stage != "prepare":
            self.index += 1
        self._last_stage = stage or ""
        within = base + span * (frac if frac is not None else 0.0)
        self.fraction = max(self.fraction, min((max(self.index, 1) - 1 + within) / self.total, 0.995 if stage != "done" or self.index < self.total else 1.0))
        self.stage = f"{label}（{self.index}/{self.total}）" if self.total > 1 else label

    def to_dict(self) -> dict[str, Any]:
        end = self.finished or time.time()
        elapsed = end - self.started
        eta = None
        if self.status == "running" and self.fraction >= 0.03:
            eta = round(elapsed * (1 - self.fraction) / self.fraction, 1)
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "stage": self.stage,
            "fraction": round(self.fraction, 4), "elapsed": round(elapsed, 1), "eta": eta, "index": self.index, "total": self.total,
            "result": self.result, "error": self.error, "error_type": self.error_type,
        }


# ---------- 服务器 ----------


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, plan_path: str | Path, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in _LOOPBACK:
            raise ValueError(f"审阅页只允许绑定本机（127.0.0.1），收到 host={host!r}。它能读写方案和素材，不该暴露到网络上。")
        self.plan_path = Path(plan_path).resolve()
        Plan.load(self.plan_path)  # 提前失败：打不开的方案不该启动一个空页面
        super().__init__(("127.0.0.1", port), ReviewHandler)
        self.plan_lock = threading.RLock()
        self._plan_cache: tuple[tuple[int, int], Plan, str] | None = None
        self.jobs: dict[str, Job] = {}
        self.jobs_lock = threading.Lock()
        self.known_files: set[str] = set()
        self._peak_locks: dict[tuple[str, int], threading.Lock] = {}
        self._thread: threading.Thread | None = None

    # -- 生命周期（测试里用）--
    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> ReviewServer:
        self._thread = threading.Thread(target=lambda: self.serve_forever(poll_interval=0.05), name="review-server", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> ReviewServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- 方案读写 --
    def read_plan(self, *, fresh: bool = False) -> tuple[Plan, str]:
        """读磁盘上的方案和它的内容哈希。fresh=False 时按 (mtime, size) 复用上次解析的结果。"""
        try:
            st = self.plan_path.stat()
        except FileNotFoundError:
            raise ApiError(HTTPStatus.NOT_FOUND, f"方案文件不见了：{self.plan_path}。它可能被移动或删除了；重新运行 autocut 生成，或把文件放回原处。") from None
        key = (st.st_mtime_ns, st.st_size)
        if not fresh and self._plan_cache and self._plan_cache[0] == key:
            return self._plan_cache[1], self._plan_cache[2]
        raw = self.plan_path.read_bytes()
        try:
            plan = Plan.load(self.plan_path)
        except PlanError as e:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"磁盘上的方案现在不合法：{e}", code="disk_invalid") from None
        etag = hashlib.sha1(raw).hexdigest()[:16]
        self._plan_cache = (key, plan, etag)
        return plan, etag

    def source_info(self, plan: Plan, sid: str) -> dict[str, Any]:
        s = plan.sources[sid]
        p = plan.source_path(sid)
        exists = p.is_file()
        return {
            "id": sid, "kind": s.kind, "path": s.path, "abs_path": str(p), "exists": exists,
            "size": p.stat().st_size if exists else None,
            "duration": s.duration, "width": s.width, "height": s.height, "fps": s.fps,
            "url": f"/media/{sid}", "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            "has_words": self.words_file(plan, sid) is not None,
            "warnings": s.warnings,
        }

    def plan_payload(self, plan: Plan, etag: str) -> dict[str, Any]:
        return {
            "plan": plan.to_dict(),
            "revision": plan.revision,
            "etag": etag,
            "summary": plan.summary(),
            "name": self.plan_path.name,
            "path": str(self.plan_path),
            "sources": {sid: self.source_info(plan, sid) for sid in plan.sources},
            "capabilities": {k: _module_available(m) for k, m in (("render", "autocut.render"), ("check", "autocut.check"), ("export", "autocut.export"))},
            "formats": list(EXPORT_FORMATS),
        }

    def words_file(self, plan: Plan, sid: str) -> Path | None:
        s = plan.sources[sid]
        if s.words:
            p = Path(s.words)
            if not p.is_absolute():
                p = self.plan_path.parent / p
            if p.is_file():
                return p
        src = plan.source_path(sid)
        if not src.is_file():
            return None
        try:
            from autocut.analysis import Media

            media = Media(src)
            hit = media.words_cache_path(None, s.analysis_track)
            if hit:
                return hit
            cands = sorted(media.cache_dir.glob("words-*.json"), key=lambda q: q.stat().st_mtime, reverse=True)
            return cands[0] if cands else None
        except Exception:  # 缓存目录读不了不该影响主流程
            log.debug("查找 %s 的文字稿缓存失败", sid, exc_info=True)
            return None

    def peak_lock(self, sid: str, per_sec: int) -> threading.Lock:
        with self.jobs_lock:
            return self._peak_locks.setdefault((sid, per_sec), threading.Lock())

    def add_job(self, job: Job) -> None:
        with self.jobs_lock:
            self.jobs[job.id] = job
            for old in [j for j in self.jobs.values() if j.finished and time.time() - j.finished > 3600]:
                self.jobs.pop(old.id, None)


def _module_available(name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _import_feature(name: str, attr: str) -> Callable[..., Any]:
    """按需 import 出片/体检/导出模块；失败返回 503 而不是让请求线程崩掉。"""
    label = _MODULE_LABEL.get(name, name)
    try:
        mod = importlib.import_module(name)
        return getattr(mod, attr)
    except ImportError as e:
        raise ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE, f"{label}模块不可用：{e}。这个功能还没装好；审阅和保存方案不受影响，可以先用命令行完成这一步。",
            code="module_unavailable", module=name,
        ) from None
    except AttributeError:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"{label}模块不可用：{name} 里没有 {attr}()。", code="module_unavailable", module=name) from None
    except Exception as e:  # 别人的模块里有语法错误等
        log.exception("导入 %s 失败", name)
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"{label}模块不可用：导入时出错 {type(e).__name__}: {e}", code="module_unavailable", module=name) from None


# ---------- 请求处理 ----------


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer
    protocol_version = "HTTP/1.1"
    server_version = "autocut-review"

    # BaseHTTPRequestHandler 默认往 stderr 写每个请求；改成 debug 日志，自己记带耗时的那一行
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("http: " + format, *args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # -- 路由 --
    def _routes(self) -> list[tuple[str, str, Callable[..., None]]]:
        return [
            ("GET", r"/api/plan", self.get_plan),
            ("PUT", r"/api/plan", self.put_plan),
            ("GET", r"/api/plan/status", self.get_status),
            ("GET", r"/media/([^/]+)", self.get_media),
            ("GET", r"/api/words/([^/]+)", self.get_words),
            ("GET", r"/api/peaks/([^/]+)", self.get_peaks),
            ("POST", r"/api/render", self.post_render),
            ("GET", r"/api/jobs/([^/]+)", self.get_job),
            ("POST", r"/api/jobs/([^/]+)/cancel", self.cancel_job),
            ("POST", r"/api/check", self.post_check),
            ("POST", r"/api/export", self.post_export),
            ("POST", r"/api/relocate", self.post_relocate),
            ("POST", r"/api/reveal", self.post_reveal),
        ]

    def _dispatch(self, method: str) -> None:
        t0 = time.perf_counter()
        self._status = 0
        self._body_read = False
        parsed = urlparse(self.path)
        self.query = parse_qs(parsed.query)
        path = parsed.path
        try:
            self._guard(method)
            lookup = "GET" if method == "HEAD" else method
            matched_path = False
            for m, pattern, fn in self._routes():
                mt = re.fullmatch(pattern, path)
                if not mt:
                    continue
                matched_path = True
                if m == lookup:
                    fn(*[unquote(g) for g in mt.groups()])
                    break
            else:
                if matched_path:
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, f"{path} 不支持 {method}")
                if method in ("GET", "HEAD") and not path.startswith("/api/"):
                    self.serve_static(path)
                else:
                    raise ApiError(HTTPStatus.NOT_FOUND, f"没有这个接口：{method} {path}")
        except ApiError as e:
            headers: dict[str, str] = {}
            if e.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
                headers["Content-Range"] = f"bytes */{e.extra.get('size', 0)}"
            if method in ("PUT", "POST") and not self._body_read:
                # 没读的请求体会污染 keep-alive 连接上的下一个请求
                headers["Connection"] = "close"
                self.close_connection = True
            self.send_json(e.status, {"error": e.message, **e.extra}, headers)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            log.exception("处理 %s %s 时出错", method, self.path)
            self.send_json(500, {"error": f"服务器内部错误：{type(e).__name__}: {e}。详情见终端日志。"})
        log.debug("%s %s -> %s %.1fms", method, self.path, self._status, (time.perf_counter() - t0) * 1000)

    def _guard(self, method: str) -> None:
        """本机服务也要防 DNS rebinding 和别的网页偷偷往 localhost 发请求。"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in _LOOPBACK:
            raise ApiError(HTTPStatus.FORBIDDEN, f"不接受 Host={host!r} 的请求；请用 http://127.0.0.1:{self.server.port}/ 访问。")
        if method in ("PUT", "POST"):
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                raise ApiError(HTTPStatus.FORBIDDEN, f"拒绝跨站请求（Origin={origin}）。")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "请求需要 Content-Type: application/json")

    # -- 响应 --
    def send_json(self, status: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._status = int(status)
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        if n > _MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"请求体 {n} 字节，超过上限 {_MAX_BODY}")
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"请求体不是合法 JSON：第 {e.lineno} 行第 {e.colno} 列 {e.msg}") from None
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求体应该是 JSON 对象")
        return data

    def serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if name == "favicon.ico":
            self._status = 204
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        p = STATIC_DIR / name
        ok = (
            re.fullmatch(r"[\w.-]+", name) and p.suffix in _STATIC_SUFFIXES and not name.endswith(".test.js")
            and not name.startswith(".") and p.is_file()
        )
        if not ok:
            raise ApiError(HTTPStatus.NOT_FOUND, f"没有这个页面文件：{path}")
        body = p.read_bytes()
        ctype = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self._status = 200
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- 方案 --
    def get_plan(self) -> None:
        plan, etag = self.server.read_plan()
        self.send_json(200, self.server.plan_payload(plan, etag))

    def get_status(self) -> None:
        plan, etag = self.server.read_plan()
        self.send_json(200, {"revision": plan.revision, "etag": etag})

    def _check_conflict(self, revision: Any, etag: Any) -> tuple[Plan, str]:
        """持有 plan_lock 时调用：客户端基于的版本必须就是磁盘上的版本。"""
        plan, disk_etag = self.server.read_plan(fresh=True)
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"请求缺少整数 revision（收到 {revision!r}）。先 GET /api/plan 取得当前 revision。")
        if revision != plan.revision or (etag and etag != disk_etag):
            log.info("保存冲突：客户端 revision=%s etag=%s，磁盘 revision=%s etag=%s", revision, etag, plan.revision, disk_etag)
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"方案在外部被修改了：你基于 revision {revision}，磁盘上已经是 revision {plan.revision}（可能是 agent 或编辑器改的）。",
                code="conflict", disk=self.server.plan_payload(plan, disk_etag),
            )
        return plan, disk_etag

    def put_plan(self) -> None:
        body = self.read_json()
        plan_dict = body["plan"] if isinstance(body.get("plan"), dict) else body
        revision = body.get("revision", plan_dict.get("revision"))
        with self.server.plan_lock:
            disk, _ = self._check_conflict(revision, body.get("etag"))
            try:
                new = Plan.from_dict(plan_dict, path=self.server.plan_path)
            except PlanError as e:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(e), code="invalid_plan") from None
            except (TypeError, ValueError, KeyError, AttributeError) as e:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"方案格式不对：{type(e).__name__}: {e}", code="invalid_plan") from None
            new.revision = disk.revision
            new.save()
            saved, etag = self.server.read_plan(fresh=True)
        log.info("PUT 方案：revision %d → %d", disk.revision, saved.revision)
        self.send_json(200, {"revision": saved.revision, "etag": etag, "summary": saved.summary(), "saved_at": time.time()})

    def post_relocate(self) -> None:
        body = self.read_json()
        sid, raw_path = body.get("source"), body.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "请给出新的素材路径（path）")
        new_path = Path(raw_path.strip()).expanduser()
        if not new_path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{new_path} 不是一个存在的文件；检查路径拼写（可以把文件拖进终端得到完整路径）。")
        with self.server.plan_lock:
            plan, _ = self._check_conflict(body.get("revision"), body.get("etag"))
            if sid not in plan.sources:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"方案里没有素材 {sid!r}；可用：{list(plan.sources)}")
            plan.sources[sid].path = str(new_path.resolve())
            plan.save()
            saved, etag = self.server.read_plan(fresh=True)
        self.send_json(200, self.server.plan_payload(saved, etag))

    # -- 素材 --
    def _source_file(self, sid: str) -> tuple[Plan, Path]:
        plan, _ = self.server.read_plan()
        if sid not in plan.sources:
            raise ApiError(HTTPStatus.NOT_FOUND, f"方案里没有素材 {sid!r}；可用：{list(plan.sources)}")
        return plan, plan.source_path(sid)

    def get_media(self, sid: str) -> None:
        _, p = self._source_file(sid)
        if not p.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, f"找不到素材 {p.name}（方案里记的路径：{p}）。文件被移动了吗？用页面上的“重新定位”指向新位置。", code="media_missing")
        size = p.stat().st_size
        rng = parse_range(self.headers.get("Range"), size)
        start, end = rng if rng else (0, size - 1)
        length = end - start + 1 if size else 0
        status = HTTPStatus.PARTIAL_CONTENT if rng else HTTPStatus.OK
        self._status = int(status)
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        try:
            with p.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(_CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 拖动进度条时浏览器会中止上一个 Range 请求，这是正常的
            self.close_connection = True

    def get_words(self, sid: str) -> None:
        plan, _ = self._source_file(sid)
        p = self.server.words_file(plan, sid)
        if p is None:
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                f"素材 {sid} 没有逐词文字稿：方案的 sources.{sid}.words 没有指定文件，分析缓存里也没有。"
                "需要语音识别模型（models/ggml-large-v3-turbo.bin）；装好后重新运行 autocut cut 就会生成。",
                code="no_words",
            )
        try:
            words = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"文字稿文件读不了：{p}（{e}）。删掉它，重新运行 autocut cut 生成。", code="words_invalid") from None
        self.send_json(200, {"source": sid, "words": words, "file": str(p)})

    def get_peaks(self, sid: str) -> None:
        plan, p = self._source_file(sid)
        try:
            per_sec = int(self.query.get("per_sec", ["50"])[0])
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "per_sec 应该是整数（每秒多少个峰值，1–400）") from None
        if not 1 <= per_sec <= 400:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"per_sec={per_sec} 超出范围 1–400")
        if not p.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, f"找不到素材 {p}，画不了波形", code="media_missing")
        s = plan.sources[sid]
        track = s.analysis_track if s.analysis_track is not None else s.audio_track
        from autocut.analysis import Media

        media = Media(p)
        try:
            with self.server.peak_lock(sid, per_sec):
                data = media._cached(f"peaks-ps{per_sec}-t{track}", lambda: _compute_peaks(media, track, per_sec))
        except Exception as e:
            log.warning("计算 %s 的波形失败：%s", p.name, e, exc_info=True)
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"算不出 {p.name} 的波形：{e}。素材可能没有音频流。", code="no_audio") from None
        self.send_json(200, {"source": sid, **data})

    # -- 出片 / 体检 / 导出 --
    def _fresh_plan(self) -> Plan:
        plan, _ = self.server.read_plan(fresh=True)
        return plan

    def post_render(self) -> None:
        body = self.read_json()
        render = _import_feature("autocut.render", "render")
        plan = self._fresh_plan()
        ids = body.get("output_ids")
        if ids is None:
            ids = [o.id for o in plan.outputs if o.selected]
        if not isinstance(ids, list) or not ids:
            raise ApiError(HTTPStatus.BAD_REQUEST, "没有要出片的输出：候选都没勾选。至少勾选一条。")
        try:
            for i in ids:
                plan.output(i)
        except PlanError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None
        job = Job("render")
        self.server.add_job(job)
        log.info("出片任务 %s 开始：%s", job.id, ids)
        threading.Thread(target=self._run_render, args=(job, render, plan, ids), name=f"render-{job.id}", daemon=True).start()
        self.send_json(202, {"job": job.id})

    def _run_render(self, job: Job, render: Callable[..., Any], plan: Plan, ids: list[str]) -> None:
        def progress(*args: Any, **kw: Any) -> None:
            if job.cancel.is_set():
                raise JobCancelled("已取消")  # render 不会吞掉它，ffmpeg 的半成品由 render 的 finally 清理
            stage, frac = _parse_progress(args, kw)
            job.update(stage, frac)

        job.total = len(ids)
        try:
            result = _call_flex(render, plan, output_ids=ids, progress=progress, cancel=job.cancel)
            job.result = _slim(_jsonify(result))
            self.server.known_files.update(_file_paths(job.result))
            job.fraction = 1.0
            job.stage = "完成"
            job.status = "done"
            log.info("出片任务 %s 完成：%.1fs", job.id, time.time() - job.started)
        except InterruptedError:
            job.status = "cancelled"
            job.stage = "已取消"
            log.info("出片任务 %s 已取消", job.id)
        except Exception as e:
            if job.cancel.is_set():
                job.status, job.stage = "cancelled", "已取消"
            else:
                job.status, job.error, job.error_type = "error", str(e) or type(e).__name__, type(e).__name__
                log.warning("出片任务 %s 失败：%s", job.id, e, exc_info=True)
        finally:
            job.finished = time.time()

    def _job(self, jid: str) -> Job:
        job = self.server.jobs.get(jid)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"没有任务 {jid}（服务重启后任务记录会丢失）")
        return job

    def get_job(self, jid: str) -> None:
        self.send_json(200, self._job(jid).to_dict())

    def cancel_job(self, jid: str) -> None:
        job = self._job(jid)
        if job.status == "running":
            job.cancel.set()
            job.stage = "正在取消…"
        self.send_json(200, job.to_dict())

    def post_check(self) -> None:
        body = self.read_json()
        check = _import_feature("autocut.check", "check")
        plan = self._fresh_plan()
        t0 = time.perf_counter()
        try:
            report = _call_flex(check, plan, output_id=body.get("output_id"))
        except PlanError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None
        except Exception as e:
            log.warning("体检失败", exc_info=True)
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"体检出错：{type(e).__name__}: {e}") from None
        log.info("体检完成：%.2fs", time.perf_counter() - t0)
        self.send_json(200, _jsonify(report))

    def post_export(self) -> None:
        body = self.read_json()
        fmt = body.get("format")
        if fmt not in EXPORT_FORMATS:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"format 应该是 {list(EXPORT_FORMATS)} 之一，收到 {fmt!r}")
        export = _import_feature("autocut.export", "export")
        plan = self._fresh_plan()
        try:
            result = _jsonify(_call_flex(export, plan, fmt, output_id=body.get("output_id")))
        except PlanError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None
        except Exception as e:
            log.warning("导出 %s 失败", fmt, exc_info=True)
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"导出 {fmt} 出错：{type(e).__name__}: {e}") from None
        paths = _file_paths(result) if not isinstance(result, str) else _file_paths(result, "path")
        self.server.known_files.update(paths)
        log.info("导出 %s：%s", fmt, paths)
        self.send_json(200, {"format": fmt, "paths": paths, "result": result})

    def post_reveal(self) -> None:
        p = str(self.read_json().get("path") or "")
        try:
            resolved = Path(p).resolve()
            inside_plan_dir = self.server.plan_path.parent in resolved.parents
        except (OSError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"路径不合法：{p!r}") from None
        if p not in self.server.known_files and not inside_plan_dir:
            raise ApiError(HTTPStatus.FORBIDDEN, "只能显示本次生成的文件或方案所在目录里的文件。")
        if not resolved.exists():
            raise ApiError(HTTPStatus.NOT_FOUND, f"{resolved} 已经不存在了")
        cmd = ["open", "-R", str(resolved)] if sys.platform == "darwin" else ["xdg-open", str(resolved.parent)]
        try:
            subprocess.run(cmd, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"打开文件管理器失败：{e}") from None
        self.send_json(200, {"ok": True})


def _compute_peaks(media: Any, track: int | None, per_sec: int) -> dict[str, Any]:
    """把音频降采样成每秒 per_sec 个峰值（每格取绝对值最大），0–1。"""
    import numpy as np

    from autocut.analysis import SR

    y = media.audio(track)
    if y.size == 0:
        return {"per_sec": per_sec, "duration": 0.0, "track": track, "peaks": []}
    n = int(np.ceil(y.size * per_sec / SR))
    edges = np.floor(np.arange(n) * SR / per_sec).astype(np.int64)
    peaks = np.maximum.reduceat(np.abs(y), edges)
    return {
        "per_sec": per_sec, "duration": round(y.size / SR, 3), "track": track,
        "peaks": np.round(np.minimum(peaks, 1.0), 3).tolist(),
    }


# ---------- 入口 ----------


def make_server(plan_path: str | Path, *, host: str = "127.0.0.1", port: int = 0) -> ReviewServer:
    """创建服务器但不启动。port=0 表示让系统挑一个空闲端口（用 server.port / server.url 取）。
    测试里：`with make_server(p) as srv: ...`，或者 srv.start() / srv.stop()。"""
    return ReviewServer(plan_path, host=host, port=port)


def serve(plan_path: str | Path, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
    """启动审阅页并阻塞到 Ctrl+C。"""
    server = make_server(plan_path, host=host, port=port)
    log.info("审阅页启动：%s（方案 %s）", server.url, server.plan_path)
    sys.stderr.write(f"审阅页：{server.url}\n方案：{server.plan_path}\n按 Ctrl+C 退出（方案已自动保存，随时可以关）。\n")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，退出")
    finally:
        server.server_close()
