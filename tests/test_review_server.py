"""审阅页服务器：在线程里起真服务，用 fixtures/media/talk_zh.mp4 构造一份小方案，走真实 HTTP。"""

from __future__ import annotations

import copy
import http.client
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from autocut.plan import Captions, Output, Plan, Segment, Source
from autocut.review.server import Job, ReviewServer, _parse_progress, make_server, parse_range

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
TALK = ROOT / "fixtures" / "media" / "talk_zh.mp4"
STATIC = ROOT / "src" / "autocut" / "review" / "static"


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """波形缓存写到 tmp，不污染 fixtures/media/.autocut-cache。"""
    monkeypatch.setenv("AUTOCUT_CACHE", str(tmp_path / "cache"))


def _make_plan(tmp_path: Path, *, source: Path = TALK) -> Path:
    """talk_zh.mp4 的真值：0–2.81 说话，2.81–3.16 停顿，3.16–9.25 说话，9.25–11.15 长停顿，11.15–14.11 说话……"""
    words = tmp_path / "talk_zh.words.json"
    words.write_text(json.dumps([{"text": "大家好", "start": 0.12, "end": 0.86, "p": 0.98}, {"text": "，", "start": 0.86, "end": 0.86, "p": 0.97}], ensure_ascii=False))
    segs = [
        Segment("d001", "main", 0.0, 2.81, "keep", "speech", text="大家好，欢迎回到我的频道。"),
        Segment("d002", "main", 2.81, 3.16, "keep", "pause", detail="停顿 0.35s · 保留（自然停顿）"),
        Segment("d003", "main", 3.16, 9.25, "keep", "speech", text="今天我们来聊一聊。"),
        Segment("d004", "main", 9.25, 11.15, "cut", "silence", 0.96, detail="停顿 1.9s → 0.4s", evidence={"gap": 1.9}),
        Segment("d005", "main", 11.15, 14.11, "keep", "speech", text="首先你需要准备好原始素材。"),
        Segment("d006", "main", 14.11, 14.51, "cut", "filler", 0.71, text="嗯", detail="口头禅"),
        Segment("d007", "main", 14.51, 20.0, "keep", "speech", text="然后打开剪辑软件。"),
    ]
    plan = Plan(
        recipe="cut",
        sources={"main": Source("main", str(source), duration=40.74, words=str(words))},
        outputs=[Output("main", segs, captions=Captions(enabled=True))],
        notes=["演示方案"],
    )
    plan.path = tmp_path / "talk_zh.autocut.json"
    plan.save()
    return plan.path


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    if not TALK.exists():
        pytest.skip("run fixtures/make_fixtures.py")
    return _make_plan(tmp_path)


@pytest.fixture
def srv(plan_path: Path):
    with make_server(plan_path) as s:
        yield s


def call(srv: ReviewServer, method: str, path: str, body: object | None = None, headers: dict[str, str] | None = None):
    """返回 (status, headers, 原始字节)。"""
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=30)
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    raw = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, raw)
    conn.close()
    return out


def jcall(srv: ReviewServer, method: str, path: str, body: object | None = None, headers: dict[str, str] | None = None):
    status, hdrs, raw = call(srv, method, path, body, headers)
    return status, json.loads(raw) if raw else None


# ---------- 方案读写 ----------


def test_get_plan(srv: ReviewServer) -> None:
    status, d = jcall(srv, "GET", "/api/plan")
    assert status == 200
    assert d["revision"] == 1  # save() 过一次
    assert d["plan"]["recipe"] == "cut"
    assert len(d["plan"]["outputs"][0]["segments"]) == 7
    assert d["summary"]["outputs"][0]["uncertain"] == ["d006"]
    src = d["sources"]["main"]
    assert src["exists"] is True and src["url"] == "/media/main" and src["kind"] == "video"
    assert src["has_words"] is True
    assert set(d["capabilities"]) == {"render", "check", "export", "repace", "captions"}
    assert d["formats"] == ["fcpxml", "xmeml", "edl", "srt", "markers"]
    assert d["etag"]


def test_put_with_current_revision_succeeds_and_bumps(srv: ReviewServer, plan_path: Path) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    plan = d["plan"]
    plan["outputs"][0]["segments"][5]["action"] = "keep"  # 把"嗯"改回保留
    status, r = jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "etag": d["etag"], "plan": plan})
    assert status == 200, r
    assert r["revision"] == d["revision"] + 1
    on_disk = Plan.load(plan_path)
    assert on_disk.revision == d["revision"] + 1
    assert on_disk.output().segments[5].action == "keep"
    # 带着新 revision / etag 可以继续保存
    plan["outputs"][0]["segments"][5]["action"] = "cut"
    status, r2 = jcall(srv, "PUT", "/api/plan", {"revision": r["revision"], "etag": r["etag"], "plan": plan})
    assert status == 200 and r2["revision"] == r["revision"] + 1


def test_put_accepts_bare_plan_with_revision(srv: ReviewServer) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    status, r = jcall(srv, "PUT", "/api/plan", d["plan"])  # 整份方案自带 revision
    assert status == 200 and r["revision"] == d["revision"] + 1


def test_put_stale_revision_returns_409_with_disk_version(srv: ReviewServer, plan_path: Path) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    # agent 抢先改了磁盘上的方案
    agent = Plan.load(plan_path)
    agent.set_action("d004", "keep")
    agent.save()
    mine = copy.deepcopy(d["plan"])
    mine["outputs"][0]["segments"][5]["action"] = "keep"
    status, r = jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "plan": mine})
    assert status == 409
    assert r["code"] == "conflict"
    assert "外部被修改" in r["error"]
    assert r["disk"]["revision"] == d["revision"] + 1
    assert r["disk"]["plan"]["outputs"][0]["segments"][3]["action"] == "keep"  # 带的是磁盘上的版本
    # 磁盘没被覆盖
    assert Plan.load(plan_path).output().segments[5].action == "cut"


def test_put_detects_hand_edit_without_revision_bump(srv: ReviewServer, plan_path: Path) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    raw = json.loads(plan_path.read_text())
    raw["notes"] = ["有人手改了这个文件，没动 revision"]
    plan_path.write_text(json.dumps(raw, ensure_ascii=False))
    status, r = jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "etag": d["etag"], "plan": d["plan"]})
    assert status == 409 and r["code"] == "conflict"
    assert r["disk"]["plan"]["notes"] == ["有人手改了这个文件，没动 revision"]


def test_put_invalid_plan_returns_400_with_planerror_text(srv: ReviewServer, plan_path: Path) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    bad = copy.deepcopy(d["plan"])
    bad["outputs"][0]["segments"][0]["action"] = "drop"
    bad["outputs"][0]["segments"][1]["end"] = 1.0  # end <= start
    status, r = jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "plan": bad})
    assert status == 400
    assert r["code"] == "invalid_plan"
    assert "方案有 2 处问题" in r["error"]
    assert "action='drop'，应为 ('keep', 'cut')" in r["error"]
    assert "必须大于 start" in r["error"]
    assert Plan.load(plan_path).revision == d["revision"]  # 没写盘


def test_put_unknown_field_is_rejected_not_ignored(srv: ReviewServer) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    bad = copy.deepcopy(d["plan"])
    bad["outputs"][0]["segments"][0]["acton"] = "cut"
    status, r = jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "plan": bad})
    assert status == 400 and "不认识字段" in r["error"] and "acton" in r["error"]


def test_put_requires_revision_and_valid_json(srv: ReviewServer) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    plan = d["plan"]
    plan.pop("revision", None)
    status, r = jcall(srv, "PUT", "/api/plan", {"plan": plan})
    assert status == 400 and "revision" in r["error"]
    status, raw_headers, raw = call(srv, "PUT", "/api/plan", headers={"Content-Type": "application/json"})
    assert status == 400  # 空请求体 → 缺 revision
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.request("PUT", "/api/plan", body=b"{not json", headers={"Content-Type": "application/json"})
    r2 = conn.getresponse()
    assert r2.status == 400 and "不是合法 JSON" in json.loads(r2.read())["error"]


def test_status_endpoint_tracks_external_changes(srv: ReviewServer, plan_path: Path) -> None:
    _, s1 = jcall(srv, "GET", "/api/plan/status")
    p = Plan.load(plan_path)
    p.save()
    _, s2 = jcall(srv, "GET", "/api/plan/status")
    assert s2["revision"] == s1["revision"] + 1 and s2["etag"] != s1["etag"]


def test_concurrent_puts_exactly_one_wins(srv: ReviewServer) -> None:
    _, d = jcall(srv, "GET", "/api/plan")
    results: list[int] = []

    def put() -> None:
        results.append(jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "plan": d["plan"]})[0])

    threads = [threading.Thread(target=put) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200] + [409] * 5


# ---------- 素材 ----------


def test_media_full_and_range(srv: ReviewServer) -> None:
    size = TALK.stat().st_size
    status, h, body = call(srv, "GET", "/media/main")
    assert status == 200 and len(body) == size
    assert h["accept-ranges"] == "bytes" and h["content-type"] == "video/mp4"

    status, h, body = call(srv, "GET", "/media/main", headers={"Range": "bytes=0-99"})
    assert status == 206
    assert h["content-range"] == f"bytes 0-99/{size}"
    assert h["content-length"] == "100" and body == TALK.read_bytes()[:100]

    status, h, body = call(srv, "GET", "/media/main", headers={"Range": "bytes=1000-"})
    assert status == 206 and h["content-range"] == f"bytes 1000-{size - 1}/{size}"
    assert body == TALK.read_bytes()[1000:]

    status, h, body = call(srv, "GET", "/media/main", headers={"Range": "bytes=-50"})
    assert status == 206 and h["content-range"] == f"bytes {size - 50}-{size - 1}/{size}" and len(body) == 50

    # 终点超过文件大小：夹到文件末尾
    status, h, body = call(srv, "GET", "/media/main", headers={"Range": f"bytes={size - 10}-{size + 999}"})
    assert status == 206 and len(body) == 10

    status, h, body = call(srv, "GET", "/media/main", headers={"Range": f"bytes={size + 5}-"})
    assert status == 416 and h["content-range"] == f"bytes */{size}"

    status, h, body = call(srv, "HEAD", "/media/main")
    assert status == 200 and h["content-length"] == str(size) and body == b""


def test_parse_range_edge_cases() -> None:
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9,20-29", 100) is None  # 多段：当作没有 Range
    assert parse_range("bytes=5-", 100) == (5, 99)
    assert parse_range("bytes=0-0", 100) == (0, 0)
    assert parse_range("bytes=-200", 100) == (0, 99)


def test_media_missing_returns_404_with_path(tmp_path: Path) -> None:
    path = _make_plan(tmp_path, source=tmp_path / "gone.mp4")
    with make_server(path) as s:
        status, r = jcall(s, "GET", "/media/main")
        assert status == 404 and r["code"] == "media_missing" and "gone.mp4" in r["error"]
        _, d = jcall(s, "GET", "/api/plan")
        assert d["sources"]["main"]["exists"] is False
        status, r = jcall(s, "GET", "/media/nope")
        assert status == 404 and "可用" in r["error"]


def test_relocate_missing_source(tmp_path: Path) -> None:
    path = _make_plan(tmp_path, source=tmp_path / "gone.mp4")
    with make_server(path) as s:
        _, d = jcall(s, "GET", "/api/plan")
        status, r = jcall(s, "POST", "/api/relocate", {"source": "main", "path": str(tmp_path / "still-gone.mp4"), "revision": d["revision"]})
        assert status == 400 and "不是一个存在的文件" in r["error"]
        status, r = jcall(s, "POST", "/api/relocate", {"source": "main", "path": str(TALK), "revision": d["revision"]})
        assert status == 200
        assert r["sources"]["main"]["exists"] is True and r["revision"] == d["revision"] + 1
        assert Plan.load(path).sources["main"].path == str(TALK.resolve())
        status, r = jcall(s, "POST", "/api/relocate", {"source": "main", "path": str(TALK), "revision": d["revision"]})
        assert status == 409  # 基于旧 revision


def test_peaks_length_and_cache(srv: ReviewServer, tmp_path: Path) -> None:
    status, d = jcall(srv, "GET", "/api/peaks/main?per_sec=50")
    assert status == 200, d
    assert d["per_sec"] == 50
    assert abs(len(d["peaks"]) - d["duration"] * 50) <= 1
    assert abs(d["duration"] - 40.74) < 0.1
    assert all(0 <= p <= 1 for p in d["peaks"]) and max(d["peaks"]) > 0.05
    # 开头 0–2.8s 是说话，9.3–11.1s 是长停顿：说话处的峰值明显更高
    talk = max(d["peaks"][5 * 5 : 5 * 50])
    gap = max(d["peaks"][int(9.5 * 50) : int(11.0 * 50)])
    assert talk > gap * 3
    assert list((tmp_path / "cache").glob("*/peaks-ps50-tNone.json"))  # 落盘缓存
    t0 = time.perf_counter()
    _, d2 = jcall(srv, "GET", "/api/peaks/main?per_sec=50")
    assert d2["peaks"] == d["peaks"] and time.perf_counter() - t0 < 1.0
    _, d3 = jcall(srv, "GET", "/api/peaks/main?per_sec=10")
    assert abs(len(d3["peaks"]) - d3["duration"] * 10) <= 1
    assert jcall(srv, "GET", "/api/peaks/main?per_sec=0")[0] == 400
    assert jcall(srv, "GET", "/api/peaks/main?per_sec=abc")[0] == 400


def test_words(srv: ReviewServer, tmp_path: Path) -> None:
    status, d = jcall(srv, "GET", "/api/words/main")
    assert status == 200 and d["words"][0]["text"] == "大家好"
    # 方案里没有指定文字稿、缓存里也没有 → 404，说明原因
    raw = json.loads(srv.plan_path.read_text())
    del raw["sources"]["main"]["words"]
    srv.plan_path.write_text(json.dumps(raw))
    status, d = jcall(srv, "GET", "/api/words/main")
    assert status == 404 and d["code"] == "no_words" and "没有逐词文字稿" in d["error"] and "语音识别模型" in d["error"]
    assert jcall(srv, "GET", "/api/words/zzz")[0] == 404


# ---------- 出片 / 体检 / 导出 ----------


@pytest.fixture
def no_feature_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("render", "check", "export"):
        monkeypatch.setitem(sys.modules, f"autocut.{name}", None)  # 令 import 抛 ImportError


def test_missing_modules_return_503_not_crash(srv: ReviewServer, no_feature_modules: None) -> None:
    status, r = jcall(srv, "POST", "/api/render", {})
    assert status == 503 and "出片模块不可用" in r["error"] and r["code"] == "module_unavailable"
    status, r = jcall(srv, "POST", "/api/check", {})
    assert status == 503 and "体检模块不可用" in r["error"]
    status, r = jcall(srv, "POST", "/api/export", {"format": "srt"})
    assert status == 503 and "导出模块不可用" in r["error"]
    assert jcall(srv, "GET", "/api/plan")[0] == 200  # 服务还活着
    _, d = jcall(srv, "GET", "/api/plan")
    assert [d["capabilities"][k] for k in ("render", "check", "export")] == [False, False, False]


def test_export_rejects_unknown_format(srv: ReviewServer) -> None:
    status, r = jcall(srv, "POST", "/api/export", {"format": "mov"})
    assert status == 400 and "fcpxml" in r["error"]


def _fake(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: object) -> None:
    mod = types.ModuleType(f"autocut.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, f"autocut.{name}", mod)


def _wait_job(srv: ReviewServer, job: str, timeout: float = 10.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        _, j = jcall(srv, "GET", f"/api/jobs/{job}")
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("任务没有在时限内结束")


def test_render_job_progress_and_result(srv: ReviewServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "talk_zh.cut.mp4"
    out.write_bytes(b"x")
    seen: dict = {}

    def render(plan, output_ids=None, progress=None):  # 签名按主线给的
        seen["ids"] = output_ids
        progress("编码", 0.5)
        progress("收尾", 0.9)
        return [{"output": "main", "path": str(out), "duration": 12.3, "sync_ok": True, "lufs": -14.2}]

    _fake(monkeypatch, "render", render=render)
    status, r = jcall(srv, "POST", "/api/render", {})
    assert status == 202
    j = _wait_job(srv, r["job"])
    assert j["status"] == "done" and j["fraction"] == 1.0
    assert j["result"][0]["duration"] == 12.3 and j["result"][0]["lufs"] == -14.2
    assert seen["ids"] == ["main"]
    assert str(out) in srv.known_files
    assert jcall(srv, "GET", "/api/jobs/nope")[0] == 404


def test_render_error_and_cancel(srv: ReviewServer, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(plan, output_ids=None, progress=None):
        raise RuntimeError("ffmpeg 无法读取第 2 条音轨：Invalid data")

    _fake(monkeypatch, "render", render=boom)
    _, r = jcall(srv, "POST", "/api/render", {})
    j = _wait_job(srv, r["job"])
    assert j["status"] == "error" and "第 2 条音轨" in j["error"]

    started = threading.Event()

    def slow(plan, output_ids=None, progress=None):
        started.set()
        for _ in range(500):
            progress("编码", 0.1)  # 取消时 progress 会抛 InterruptedError
            time.sleep(0.01)

    _fake(monkeypatch, "render", render=slow)
    _, r = jcall(srv, "POST", "/api/render", {})
    assert started.wait(5)
    status, j = jcall(srv, "POST", f"/api/jobs/{r['job']}/cancel", {})
    assert status == 200
    j = _wait_job(srv, r["job"])
    assert j["status"] == "cancelled"


def test_render_no_selected_outputs_is_400(srv: ReviewServer, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, "render", render=lambda plan, output_ids=None, progress=None: [])
    _, d = jcall(srv, "GET", "/api/plan")
    d["plan"]["outputs"][0]["selected"] = False
    assert jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "plan": d["plan"]})[0] == 200
    status, r = jcall(srv, "POST", "/api/render", {})
    assert status == 400 and "没有要出片" in r["error"]
    assert jcall(srv, "POST", "/api/render", {"output_ids": ["zzz"]})[0] == 400


def test_check_and_export_adapters(srv: ReviewServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Report:
        def to_dict(self) -> dict:
            return {"items": [{"at": 12.0, "severity": "high", "title": "开头太慢"}], "disclaimer": "这些是留存风险信号，不是爆款保证"}

    _fake(monkeypatch, "check", check=lambda plan, output_id=None: Report())
    status, r = jcall(srv, "POST", "/api/check", {})
    assert status == 200 and r["items"][0]["at"] == 12.0

    srt = tmp_path / "talk_zh.srt"
    srt.write_text("1\n")
    _fake(monkeypatch, "export", export=lambda plan, fmt: srt)  # 没有 output_id 参数也不会被硬塞
    status, r = jcall(srv, "POST", "/api/export", {"format": "srt"})
    assert status == 200 and r["paths"] == [str(srt)] and r["format"] == "srt"

    def bad_export(plan, fmt):
        raise ValueError("方案里没有字幕可导出")

    _fake(monkeypatch, "export", export=bad_export)
    status, r = jcall(srv, "POST", "/api/export", {"format": "srt"})
    assert status == 400 and "没有字幕可导出" in r["error"]  # ValueError：说明已经具体，原样给用户

    def crash(plan, fmt):
        raise RuntimeError("磁盘写满了")

    _fake(monkeypatch, "export", export=crash)
    status, r = jcall(srv, "POST", "/api/export", {"format": "srt"})
    assert status == 500 and "磁盘写满了" in r["error"]


# ---------- 安全 / 静态文件 ----------


def test_refuses_non_loopback_bind(plan_path: Path) -> None:
    with pytest.raises(ValueError, match="只允许绑定本机"):
        make_server(plan_path, host="0.0.0.0")
    with make_server(plan_path, host="localhost") as s:
        assert s.server_address[0] == "127.0.0.1"


def test_rejects_foreign_host_origin_and_non_json_writes(srv: ReviewServer) -> None:
    assert call(srv, "GET", "/api/plan", headers={"Host": "evil.example:80"})[0] == 403  # DNS rebinding
    assert call(srv, "PUT", "/api/plan", {}, headers={"Origin": "http://evil.example"})[0] == 403
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.request("POST", "/api/render", body=b"x=1", headers={"Content-Type": "text/plain"})
    r = conn.getresponse()
    r.read()
    assert r.status == 415


def test_unreadable_plan_fails_fast(tmp_path: Path) -> None:
    from autocut.plan import PlanError

    bad = tmp_path / "bad.autocut.json"
    bad.write_text("{oops")
    with pytest.raises(PlanError, match="不是合法 JSON"):
        make_server(bad)


def test_static_files_and_traversal(srv: ReviewServer) -> None:
    if not (STATIC / "index.html").exists():
        pytest.skip("static/index.html 还没写")
    status, h, body = call(srv, "GET", "/")
    assert status == 200 and h["content-type"].startswith("text/html") and b"<html" in body.lower()
    status, h, _ = call(srv, "GET", "/model.js")
    assert status == 200 and h["content-type"].startswith("text/javascript")
    assert call(srv, "GET", "/model.test.js")[0] == 404
    assert call(srv, "GET", "/testdata/move_boundary.golden.json")[0] == 404
    assert call(srv, "GET", "/..%2F..%2Fplan.py")[0] == 404
    assert call(srv, "GET", "/%2e%2e/server.py")[0] == 404
    assert call(srv, "GET", "/api/nope")[0] == 404
    assert call(srv, "GET", "/favicon.ico")[0] == 204


# ---------- 与 plan.move_boundary 的一致性 ----------


@pytest.mark.unit
def test_move_boundary_golden_matches_python() -> None:
    """model.test.js 用同一份 golden 校验 JS 实现；这里保证 golden 本身就是 Python 当前的行为。"""
    from autocut.plan import PlanError

    g = json.loads((STATIC / "testdata" / "move_boundary.golden.json").read_text())
    assert len(g["cases"]) == len(g["expected"]) >= 10
    for c, exp in zip(g["cases"], g["expected"]):
        plan = Plan.from_dict(copy.deepcopy(g["plan"]))
        try:
            plan.move_boundary(c["seg"], c["edge"], c["t"])
        except PlanError as e:
            assert exp == {"error": str(e)}, c
        else:
            assert exp == {"segments": [[s.start, s.end] for s in plan.outputs[0].segments]}, c


# ---------- 进度折算、文字稿缓存、真实的出片/导出模块 ----------


@pytest.mark.unit
def test_job_progress_maps_render_stages_over_multiple_outputs() -> None:
    """render 的 fraction 是“当前输出”的 0–1，每个输出都从 prepare 重来；整体进度必须单调不减、最终到 1。"""
    job = Job("render")
    job.total = 2
    seen: list[float] = []
    for _ in range(2):
        for stage, fracs in (("prepare", [0.0]), ("measure", [0.0, 1.0]), ("encode", [0.0, 0.5, 1.0]), ("verify", [0.0]), ("done", [1.0])):
            for f in fracs:
                job.update(stage, f)
                seen.append(job.fraction)
    assert seen == sorted(seen)
    assert job.index == 2 and 0.99 <= seen[-1] <= 1.0
    assert "2/2" in job.stage or job.stage == "完成（2/2）"
    mid = Job("render")
    mid.total = 2
    for stage, f in (("prepare", 0.0), ("encode", 1.0), ("done", 1.0)):
        mid.update(stage, f)
    assert 0.45 < mid.fraction < 0.55  # 第一个输出完成 → 约一半


@pytest.mark.unit
def test_parse_progress_accepts_several_shapes() -> None:
    assert _parse_progress(("encode", 0.4), {}) == ("encode", 0.4)
    assert _parse_progress((0.25,), {}) == (None, 0.25)
    assert _parse_progress(({"stage": "x", "percent": 50},), {}) == ("x", 0.5)
    assert _parse_progress((), {"stage": "y", "fraction": 7}) == ("y", 1.0)  # 夹在 0–1


def test_words_found_in_analysis_cache_when_plan_has_no_words_field(plan_path: Path, tmp_path: Path) -> None:
    from autocut.analysis import Media

    raw = json.loads(plan_path.read_text())
    del raw["sources"]["main"]["words"]
    plan_path.write_text(json.dumps(raw))
    media = Media(TALK)
    media.cache_dir.mkdir(parents=True, exist_ok=True)
    (media.cache_dir / "words-auto-tNone-test.json").write_text(json.dumps([{"text": "缓存里的词", "start": 0.1, "end": 0.5, "p": 0.9}], ensure_ascii=False))
    with make_server(plan_path) as s:
        status, d = jcall(s, "GET", "/api/words/main")
        assert status == 200 and d["words"][0]["text"] == "缓存里的词"
        assert jcall(s, "GET", "/api/plan")[1]["sources"]["main"]["has_words"] is True


def test_real_render_and_export_modules(srv: ReviewServer, tmp_path: Path) -> None:
    """不用假模块：走 autocut.render / autocut.export 的真实签名，确认适配层和进度、结果形状都对得上。"""
    pytest.importorskip("autocut.render")
    pytest.importorskip("autocut.export")
    status, r = jcall(srv, "POST", "/api/render", {})
    assert status == 202
    j = _wait_job(srv, r["job"], timeout=120)
    assert j["status"] == "done", j
    res = j["result"][0]
    assert res["output_id"] == "main" and res["path"].endswith(".mp4") and Path(res["path"]).exists()
    assert res["duration"] and abs(res["duration"] - res["expected_duration"]) < 0.1
    assert "filter_graph" not in res and "command" not in res  # 排错用的长字段不发给页面
    assert j["fraction"] == 1.0 and j["total"] == 1
    assert res["path"] in srv.known_files

    status, r = jcall(srv, "POST", "/api/export", {"format": "edl", "output_id": "main"})
    assert status == 200 and r["paths"] and Path(r["paths"][0]).suffix == ".edl" and Path(r["paths"][0]).exists()
    # 导出模块自己的 ValueError（这份方案没有文字稿 → 导不了 srt）原样变成 400
    status, r = jcall(srv, "POST", "/api/export", {"format": "srt"})
    assert status in (200, 400)


# ---------- 改节奏、字幕、出片选项、合并保存（HCI 检查后新增） ----------


def _stub_repace(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    import autocut.recipes.cut as cut

    monkeypatch.setattr(cut, "repace", fn, raising=False)


def _get(srv: ReviewServer) -> dict:
    return jcall(srv, "GET", "/api/plan")[1]


def test_repace_endpoint_saves_and_returns_new_plan(srv: ReviewServer, plan_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    def repace(plan, pace, output_id=None):
        calls.append((pace, output_id))
        seg = plan.output(output_id).find("d004")[1]
        seg.start += 0.2  # 把长停顿的剪口往后挪：多留 0.2s
        plan.output(output_id).find("d003")[1].end += 0.2
        return 2

    _stub_repace(monkeypatch, repace)
    d = _get(srv)
    status, r = jcall(srv, "POST", "/api/repace", {"pace": "relaxed", "revision": d["revision"], "etag": d["etag"], "output_id": "main"})
    assert status == 200, r
    assert r["changed"] == 2 and calls == [("relaxed", "main")]
    assert r["revision"] == d["revision"] + 1
    on_disk = Plan.load(plan_path)
    assert on_disk.revision == r["revision"]
    assert on_disk.options["pace"] == "relaxed"
    assert on_disk.output().find("d004")[1].start == pytest.approx(9.45)
    assert r["plan"]["outputs"][0]["segments"][3]["start"] == pytest.approx(9.45)
    # 数字秒数也行；revision 过期 → 409，磁盘不动
    status, r2 = jcall(srv, "POST", "/api/repace", {"pace": 0.3, "revision": d["revision"]})
    assert status == 409 and r2["code"] == "conflict"
    assert Plan.load(plan_path).options["pace"] == "relaxed"


def test_repace_errors_are_reported_not_saved(srv: ReviewServer, plan_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _get(srv)

    def bad_pace(plan, pace, output_id=None):
        raise ValueError(f"pace={pace!r} 不认识；可选 tight / natural / relaxed")

    _stub_repace(monkeypatch, bad_pace)
    status, r = jcall(srv, "POST", "/api/repace", {"pace": "fast", "revision": d["revision"]})
    assert status == 400 and r["code"] == "invalid_pace" and "不认识" in r["error"]

    def breaks_plan(plan, pace, output_id=None):
        plan.output(output_id).segments[0].end = -1  # 写出不合法的方案
        return 1

    _stub_repace(monkeypatch, breaks_plan)
    status, r = jcall(srv, "POST", "/api/repace", {"pace": "tight", "revision": d["revision"]})
    assert status == 500 and r["code"] == "repace_invalid" and "没有保存" in r["error"]
    assert Plan.load(plan_path).revision == d["revision"]  # 磁盘没被写坏

    assert jcall(srv, "POST", "/api/repace", {"pace": True, "revision": d["revision"]})[0] == 400
    assert jcall(srv, "POST", "/api/repace", {"revision": d["revision"]})[0] == 400


def test_repace_module_missing_is_503(srv: ReviewServer, monkeypatch: pytest.MonkeyPatch) -> None:
    import autocut.recipes.cut as cut

    monkeypatch.delattr(cut, "repace", raising=False)
    d = _get(srv)
    status, r = jcall(srv, "POST", "/api/repace", {"pace": "tight", "revision": d["revision"]})
    assert status == 503 and "节奏模块不可用" in r["error"]
    assert d["capabilities"]["repace"] is False


def test_real_repace_skips_edited_and_locked_gaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用真实的 cut 配方和 repace：锁定的停顿不动，没锁的按新节奏变；页面说的“锁定的会保留”和实际行为一致。"""
    cut = pytest.importorskip("autocut.recipes.cut")
    if not hasattr(cut, "repace"):
        pytest.skip("recipes.cut.repace 还没合并")
    monkeypatch.setenv("AUTOCUT_CACHE", str(ROOT / "fixtures" / "media" / ".autocut-cache"))
    try:
        built = cut.build([TALK])
    except Exception as e:  # noqa: BLE001 —— 没有语音识别模型时配方起不来
        pytest.skip(f"cut.build 不可用：{e}")
    gaps = [s for s in built.output().segments if s.evidence.get("gap_span")]
    if len(gaps) < 2:
        pytest.skip("这份方案没有可重排的停顿")
    built.path = tmp_path / "talk.autocut.json"
    locked = gaps[0]
    locked.locked = True
    built.save()
    with make_server(built.path) as s:
        d = _get(s)
        status, r = jcall(s, "POST", "/api/repace", {"pace": "relaxed", "revision": d["revision"], "etag": d["etag"]})
        assert status == 200, r
        after = Plan.load(built.path)
        assert after.options["pace"] == "relaxed" and after.options["keep_pause"] == 0.8
        new_locked = after.output().find(locked.id)[1]
        assert (new_locked.start, new_locked.end, new_locked.action) == (locked.start, locked.end, locked.action)
        assert r["changed"] > 0
        assert after.output().segments[-1].end == pytest.approx(built.output().segments[-1].end)  # 首尾仍然相接，没丢内容


def test_captions_endpoint_uses_cues_for_and_never_triggers_asr(srv: ReviewServer, plan_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status, r = jcall(srv, "GET", "/api/captions?output=main")
    assert status == 200 and r["has_words"] is True and r["output"] == "main"
    assert r["cues"] and r["cues"][0]["text"] == "大家好"  # 方案里的文字稿只有“大家好”
    assert r["cues"][0]["start"] == pytest.approx(0.12, abs=0.05)
    assert r["enabled"] is True and r["burn"] is False
    assert jcall(srv, "GET", "/api/captions?output=nope")[0] == 400

    # 没有逐词文字稿：不调用 cues_for（它在没有缓存时会跑语音识别），告诉页面原因
    raw = json.loads(plan_path.read_text())
    del raw["sources"]["main"]["words"]
    plan_path.write_text(json.dumps(raw))
    import autocut.captions as captions

    def boom(plan, output):
        raise AssertionError("没有文字稿时不该调用 cues_for")

    monkeypatch.setattr(captions, "cues_for", boom)
    status, r = jcall(srv, "GET", "/api/captions")
    assert status == 200 and r["cues"] == [] and r["has_words"] is False and "没有逐词文字稿" in r["reason"]


def test_render_passes_loudnorm_option_only_when_given(srv: ReviewServer, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def render(plan, output_ids=None, progress=None, loudnorm="unset"):
        seen.append({"loudnorm": loudnorm})
        return []

    _fake(monkeypatch, "render", render=render)
    for body, expect in (({}, "unset"), ({"options": {"loudnorm": None}}, "unset"), ({"options": {"loudnorm": True}}, True), ({"options": {"loudnorm": False}}, False), ({"options": {"loudnorm": -16}}, -16)):
        status, r = jcall(srv, "POST", "/api/render", body)
        assert status == 202, body
        _wait_job(srv, r["job"])
        assert seen[-1]["loudnorm"] == expect, body
    assert jcall(srv, "POST", "/api/render", {"options": {"loudnorm": "loud"}})[0] == 400
    assert jcall(srv, "POST", "/api/render", {"options": [1]})[0] == 400


def test_merged_save_after_409_keeps_both_sides(srv: ReviewServer, plan_path: Path) -> None:
    """页面做三方合并时走的服务器流程：保存撞上 409 → 用 409 带回的磁盘版本合并 → 以磁盘 revision 再保存，两边的改动都在。"""
    d = _get(srv)
    mine = copy.deepcopy(d["plan"])
    mine["outputs"][0]["segments"][5]["action"] = "keep"  # 我：把“嗯”改回保留
    agent = Plan.load(plan_path)
    agent.set_action("d004", "keep")  # agent：把长停顿改回保留
    agent.save()
    status, r = jcall(srv, "PUT", "/api/plan", {"revision": d["revision"], "plan": mine})
    assert status == 409
    disk = r["disk"]
    merged = copy.deepcopy(disk["plan"])
    merged["outputs"][0]["segments"][5]["action"] = "keep"  # 合并 = agent 的版本 + 我的改动
    status, r2 = jcall(srv, "PUT", "/api/plan", {"revision": disk["revision"], "etag": disk["etag"], "plan": merged})
    assert status == 200 and r2["revision"] == disk["revision"] + 1
    final = Plan.load(plan_path)
    assert final.output().find("d004")[1].action == "keep" and final.output().find("d006")[1].action == "keep"


def test_many_parallel_connections_are_not_reset(srv: ReviewServer) -> None:
    """页面一次并行拉十几个模块：监听队列太小会 ERR_CONNECTION_RESET（审阅页白屏）。"""
    assert srv.request_queue_size >= 64
    errors: list[str] = []

    def hit() -> None:
        try:
            status, _, _ = call(srv, "GET", "/api/plan/status")
            assert status == 200
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=hit) for _ in range(60)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
