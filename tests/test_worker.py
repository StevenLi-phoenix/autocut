"""worker：策略（白名单/路径限定）、run 生命周期（排队/互斥/取消/恢复）、HTTP 路由、ingest 登记、上传与下载。

用假的 autocut 可执行脚本，不依赖 ffmpeg / 素材。
"""

from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

from autocut.worker import ingest, policy
from autocut.worker.config import WorkerConfig
from autocut.worker.policy import PolicyError
from autocut.worker.runner import Runner, build_env
from autocut.worker.server import App, make_server
from autocut.worker.store import Store

pytestmark = pytest.mark.unit

FAKE = textwrap.dedent(
    """
    import json, signal, sys, time
    args = sys.argv[1:]
    assert args[0] == "-v"
    cmd, rest = args[1], args[2:]
    sys.stderr.write(f"pid={__import__('os').getpid()}\\n"); sys.stderr.flush()
    if cmd == "render":
        def term(*_):
            print(json.dumps({"ok": False, "error": {"code": "cancelled", "message": "x", "fix": "", "retry": True}})); sys.exit(5)
        signal.signal(signal.SIGTERM, term)
        sys.stderr.write("encode 10%\\n"); sys.stderr.flush()
        time.sleep(float(rest[0]) if rest else 30)
    elif cmd == "fail":
        pass
    if cmd == "check":
        print(json.dumps({"ok": False, "error": {"code": "bad", "message": "m", "fix": "f", "retry": False}})); sys.exit(3)
    time.sleep(float(rest[0]) if cmd == "cut" and rest and rest[0].replace(".", "").isdigit() else 0)
    print(json.dumps({"ok": True, "command": cmd, "args": rest}))
    """
)


@pytest.fixture
def cfg(tmp_path: Path) -> WorkerConfig:
    fake = tmp_path / "fake_autocut.py"
    fake.write_text(FAKE)
    root = tmp_path / "root"
    root.mkdir()
    return WorkerConfig(root=root, autocut_cmd=(sys.executable, str(fake)), nice=0, port=0)


@pytest.fixture
def app(cfg: WorkerConfig) -> App:
    return App(cfg)


def wait_done(app: App, job: str, run: str, t: float = 15.0) -> dict[str, Any]:
    end = time.time() + t
    while time.time() < end:
        r = app.runner.get(job, run, wait=1)
        if r["status"] in ("ok", "failed", "cancelled"):
            return r
    raise AssertionError("run 超时")


# ------------------------------------------------------------------ policy


def test_command_whitelist(tmp_path: Path) -> None:
    for ok in ("cut", "render", "show", "check", "transcript", "clip"):
        policy.check_command(ok, [], tmp_path)
    for bad in ("review", "ytpmv", "rm", "bash", ""):
        with pytest.raises(PolicyError) as e:
            policy.check_command(bad, [], tmp_path)
        assert e.value.code == "command_not_allowed"


@pytest.mark.parametrize("arg", ["/etc/passwd", "../x.mp4", "src/../../x", "~/a.mp4", "--out-dir=/tmp/x", "--out-dir=../o"])
def test_paths_outside_job_rejected(tmp_path: Path, arg: str) -> None:
    with pytest.raises(PolicyError) as e:
        policy.check_command("cut", [arg], tmp_path)
    assert e.value.code == "path_outside_job"
    assert "job 目录" in e.value.message


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    (tmp_path / "evil").symlink_to("/etc")
    with pytest.raises(PolicyError):
        policy.check_command("cut", ["evil/passwd"], tmp_path)


@pytest.mark.parametrize("args", [["src/talk.mp4", "--pace", "natural"], ["plan.json", "c1", "--keep"], ["--pace=0.4", "x.mp4"], ["-n", "3", "--reframe", "center"], ["标题/副题"]])
def test_normal_args_accepted(tmp_path: Path, args: list[str]) -> None:
    assert policy.check_command("cut", args, tmp_path) == args


def test_job_id_rules() -> None:
    for bad in ("", "../x", "a/b", ".hidden", "x" * 70, "a b"):
        with pytest.raises(PolicyError):
            policy.check_job_id(bad)
    assert policy.check_job_id("20261031-120000-talk") == "20261031-120000-talk"


def test_reserved_job_names(app: App) -> None:
    with pytest.raises(PolicyError):
        app.store.job_dir("models")


# ------------------------------------------------------------------ env


def test_build_env_is_clean(cfg: WorkerConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "y")
    cfg2 = WorkerConfig(root=cfg.root, ffmpeg_dir="/priv/ffmpeg/bin", encoder="libx264")
    env = build_env(cfg2)
    assert env["PATH"].startswith("/priv/ffmpeg/bin:")
    assert env["AUTOCUT_ENCODER"] == "libx264"
    assert "SECRET_TOKEN" not in env and "DEEPSEEK_API_KEY" not in env


# ------------------------------------------------------------------ runner


def test_run_lifecycle_ok(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    run = app.runner.submit(job, "show", ["plan.json"])
    r = wait_done(app, job, run["id"])
    assert r["status"] == "ok" and r["result"]["command"] == "show" and r["exit_code"] == 0
    assert r["result"]["args"] == ["plan.json"]


def test_run_failure_reports_error(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    r = wait_done(app, job, app.runner.submit(job, "check", [])["id"])
    assert r["status"] == "failed" and r["exit_code"] == 3 and r["result"]["error"]["code"] == "bad"


def test_cancel_running_by_pid(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    run = app.runner.submit(job, "render", ["30"])
    for _ in range(100):
        if app.runner.get(job, run["id"])["status"] == "running":
            break
        time.sleep(0.05)
    t0 = time.time()
    app.runner.cancel(job, run["id"])
    r = wait_done(app, job, run["id"])
    assert r["status"] == "cancelled" and time.time() - t0 < 10


def test_heavy_jobs_are_serialized_and_queued_cancel(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    a = app.runner.submit(job, "render", ["30"])
    time.sleep(0.5)
    b = app.runner.submit(job, "cut", ["x.mp4"])
    time.sleep(0.8)
    assert app.runner.get(job, b["id"])["status"] == "queued"  # 重活互斥：a 占着槽位
    light = app.runner.submit(job, "show", [])  # 轻活不排队
    assert wait_done(app, job, light["id"])["status"] == "ok"
    app.runner.cancel(job, b["id"])
    assert wait_done(app, job, b["id"])["status"] == "cancelled"
    app.runner.cancel(job, a["id"])
    assert wait_done(app, job, a["id"])["status"] == "cancelled"


def test_progress_is_last_stderr_line(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    run = app.runner.submit(job, "render", ["5"])
    for _ in range(100):
        r = app.runner.get(job, run["id"])
        if r["status"] == "running" and "encode" in r.get("progress", {}).get("last", ""):
            break
        time.sleep(0.05)
    assert "encode 10%" in r["progress"]["last"]
    app.runner.cancel(job, run["id"])
    wait_done(app, job, run["id"])


def test_recover_marks_stale_runs_failed(cfg: WorkerConfig) -> None:
    store = Store(cfg.root)
    job = store.create_job("t", {})["id"]
    store.save_run(job, {"id": "r-000000-aaaaaa", "job": job, "status": "running", "pid": 99999999})
    Runner(cfg, store)
    r = store.get_run(job, "r-000000-aaaaaa")
    assert r["status"] == "failed" and "worker 重启" in r["error"]


def test_get_wait_returns_when_done(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    run = app.runner.submit(job, "cut", ["0.6"])
    t0 = time.time()
    r = app.runner.get(job, run["id"], wait=20)
    assert r["status"] == "ok" and time.time() - t0 < 10


# ------------------------------------------------------------------ ingest / provenance


def test_register_source_and_credits(app: App) -> None:
    job = app.store.create_job("t", {"attribution": {"repost_label": True, "text": "仅供学习"}})["id"]
    ingest.register_source(app.store, job, {"kind": "url", "file": "src/a.mp4", "url": "https://youtu.be/x", "title": "标题", "channel": "某频道"})
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/b.mp4"})
    prov = json.loads((app.store.job_dir(job) / "provenance.json").read_text())
    assert [s["id"] for s in prov["sources"]] == ["s1", "s2"]
    text = ingest.credits_text(app.store, job)
    assert "转载自 某频道《标题》" in text and "https://youtu.be/x" in text and "仅供学习" in text


def test_credits_empty_when_not_required(app: App) -> None:
    job = app.store.create_job("t", {"attribution": {"repost_label": False}})["id"]
    ingest.register_source(app.store, job, {"kind": "url", "file": "src/a.mp4", "url": "https://youtu.be/x"})
    assert ingest.credits_text(app.store, job) == ""


def test_ingest_env_only_whitelisted(tmp_path: Path, cfg: WorkerConfig) -> None:
    envf = tmp_path / ".env"
    envf.write_text('DEEPSEEK_API_KEY=sk-secret\nKAOROU_YT_COOKIES_PATH="/c/cookies.txt"\nexport KAOROU_POT_BASE_URL=http://127.0.0.1:4416\nKAOROU_INTERNAL_SECRET=zzz\n')
    c = WorkerConfig(root=cfg.root, ingest_env_file=str(envf))
    env = ingest.ingest_env(c)
    assert env["KAOROU_YT_COOKIES_PATH"] == "/c/cookies.txt" and env["KAOROU_POT_BASE_URL"] == "http://127.0.0.1:4416"
    assert "DEEPSEEK_API_KEY" not in env and "KAOROU_INTERNAL_SECRET" not in env
    assert env["KAOROU_MIN_HEIGHT"] == "0"  # 关掉烤肉的分辨率门槛


def test_inbox_only(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    (app.cfg.root / "inbox").mkdir()
    (app.cfg.root / "inbox" / "a.mp4").write_bytes(b"x")
    e = ingest.from_inbox(app.cfg, app.store, job, "a.mp4")
    assert e["file"] == "src/a.mp4" and (app.store.job_dir(job) / "src" / "a.mp4").exists()
    for bad in ("../job.json", "/etc/passwd", "nope.mp4"):
        with pytest.raises(PolicyError):
            ingest.from_inbox(app.cfg, app.store, job, bad)


def test_ingest_url_needs_config_and_valid_url(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    with pytest.raises(PolicyError) as e:
        app.handle("POST", f"/v1/jobs/{job}/ingest", {}, {"url": "file:///etc/passwd"})
    assert e.value.code == "bad_url"
    with pytest.raises(PolicyError) as e:
        app.handle("POST", f"/v1/jobs/{job}/ingest", {}, {"url": "https://youtu.be/abc"})
    assert e.value.code == "ingest_unavailable"


def test_ingest_url_runs_helper_and_registers(tmp_path: Path, cfg: WorkerConfig) -> None:
    """用假的"烤肉 python"（一个读参数写元数据的脚本）验证：下载 run → 完成后自动登记 provenance。"""
    fake_py = tmp_path / "fakepy.sh"
    fake_py.write_text(
        "#!/bin/bash\n# argv: helper.py --url U --out D --meta M\nout=$4; meta=$6; url=$2\n"
        'echo vid > "$5/vid1.mp4"\n'
        'echo \'{"ok": true, "video_path": "\'"$5"\'/vid1.mp4", "video_id": "vid1", "title": "T", "channel": "C", "description": "", "live_status": null, "subtitle_path": null}\' > "$7"\n'
        'cat "$7"\n'
    )
    fake_py.chmod(0o755)
    c = WorkerConfig(root=cfg.root, autocut_cmd=cfg.autocut_cmd, nice=0, port=0, ingest_python=str(fake_py))
    app = App(c)
    job = app.store.create_job("t", {})["id"]
    code, body = 200, app.handle("POST", f"/v1/jobs/{job}/ingest", {}, {"url": "https://youtu.be/abc"})[1]
    r = wait_done(app, job, body["run"]["id"])
    assert r["status"] == "ok", r
    time.sleep(0.3)
    prov = json.loads((app.store.job_dir(job) / "provenance.json").read_text())
    assert prov["sources"][0]["url"] == "https://youtu.be/abc" and prov["sources"][0]["title"] == "T" and prov["sources"][0]["file"] == "src/vid1.mp4"


# ------------------------------------------------------------------ http


@pytest.fixture
def http(app: App) -> Any:
    srv = make_server(app.cfg, app)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method: str, path: str, body: Any = None, raw: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = r.read()
                ct = r.headers.get("Content-Type", "")
                return r.status, (json.loads(payload) if "json" in ct else payload), dict(r.headers)
        except urllib.error.HTTPError as e:
            payload = e.read()
            return e.code, json.loads(payload), dict(e.headers)

    yield call
    srv.shutdown()


def test_http_job_run_flow(http: Any) -> None:
    code, b, _ = http("GET", "/health")
    assert code == 200 and b["ok"] and "cut" in b["commands"]
    code, b, _ = http("POST", "/v1/jobs", {"name": "demo", "config": {"purpose": "p"}})
    job = b["job"]["id"]
    code, b, _ = http("POST", f"/v1/jobs/{job}/run", {"cmd": "show", "args": ["plan.json"]})
    rid = b["run"]["id"]
    code, b, _ = http("GET", f"/v1/jobs/{job}/runs/{rid}?wait=10")
    assert b["run"]["status"] == "ok"
    code, b, _ = http("GET", f"/v1/jobs/{job}")
    assert b["job"]["config"]["purpose"] == "p" and len(b["runs"]) == 1


def test_http_policy_errors(http: Any) -> None:
    _, b, _ = http("POST", "/v1/jobs", {"name": "demo"})
    job = b["job"]["id"]
    code, b, _ = http("POST", f"/v1/jobs/{job}/run", {"cmd": "cut", "args": ["/etc/passwd"]})
    assert code == 400 and b["error"]["code"] == "path_outside_job" and b["error"]["fix"]
    code, b, _ = http("POST", f"/v1/jobs/{job}/run", {"cmd": "review", "args": []})
    assert code == 400 and b["error"]["code"] == "command_not_allowed"
    code, b, _ = http("GET", "/v1/jobs/nope")
    assert code == 404
    code, b, _ = http("GET", "/nothing")
    assert code == 404


def test_http_upload_and_download_with_range(http: Any) -> None:
    _, b, _ = http("POST", "/v1/jobs", {"name": "demo"})
    job = b["job"]["id"]
    payload = bytes(range(256)) * 40
    code, b, _ = http("PUT", f"/v1/jobs/{job}/upload?name=talk.mp4", raw=payload, headers={"Content-Length": str(len(payload))})
    assert code == 200 and b["source"]["file"] == "src/talk.mp4" and b["source"]["kind"] == "upload"
    code, got, _ = http("GET", f"/v1/jobs/{job}/file?path=src/talk.mp4")
    assert code == 200 and got == payload
    code, got, h = http("GET", f"/v1/jobs/{job}/file?path=src/talk.mp4", headers={"Range": "bytes=10-19"})
    assert code == 206 and got == payload[10:20] and h["Content-Range"].startswith("bytes 10-19/")
    # 逃逸与隐藏文件
    for p in ("../../etc/passwd", "job.json".replace("job", ".runs/x"), "/etc/passwd"):
        code, b, _ = http("GET", f"/v1/jobs/{job}/file?path={p}")
        assert code in (400, 404) and not b["ok"]
    code, b, _ = http("PUT", f"/v1/jobs/{job}/upload?name=evil.sh", raw=b"x", headers={"Content-Length": "1"})
    assert code == 400


def test_http_upload_name_sanitized(http: Any) -> None:
    _, b, _ = http("POST", "/v1/jobs", {"name": "demo"})
    job = b["job"]["id"]
    code, b, _ = http("PUT", f"/v1/jobs/{job}/upload?name=../../x%20y.mp4", raw=b"abc", headers={"Content-Length": "3"})
    assert code == 200 and b["source"]["file"] == "src/x_y.mp4"


# ------------------------------------------------------------------ delivery（确认门）

from autocut.worker import delivery  # noqa: E402


def _awaiting(app: App) -> str:
    job = app.store.create_job("t", {"attribution": {"repost_label": True}})["id"]
    d = app.store.job_dir(job)
    (d / "out").mkdir(exist_ok=True)
    (d / "out" / "a.main.mp4").write_bytes(b"fake")
    ingest.register_source(app.store, job, {"kind": "url", "file": "src/a.mp4", "url": "https://youtu.be/x", "title": "T", "channel": "C"})
    delivery.mark_awaiting(app.store, job, [{"id": "main", "final": "out/a.main.mp4", "preview": "out/p.mp4", "extras": []}], "摘要")
    return job


def test_handoff_blocked_until_confirmed(app: App) -> None:
    job = _awaiting(app)
    for target in ("local", "kaorou"):
        with pytest.raises(PolicyError) as e:
            delivery.handoff(app.store, job, target, "", "", app.cfg.root)
        assert e.value.code == "not_confirmed"
    assert not (app.cfg.root / "handoff").exists()


def test_confirm_requires_quote_and_awaiting(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    with pytest.raises(PolicyError) as e:
        delivery.confirm(app.store, job, "好的")
    assert e.value.code == "not_awaiting"
    job = _awaiting(app)
    with pytest.raises(PolicyError) as e:
        delivery.confirm(app.store, job, "  ")
    assert e.value.code == "quote_required"
    st = delivery.confirm(app.store, job, "可以，发布吧")
    assert st["status"] == "confirmed" and st["history"][-1]["quote"] == "可以，发布吧"


def test_confirmed_handoff_local_copies_files_and_credits(app: App) -> None:
    job = _awaiting(app)
    delivery.confirm(app.store, job, "确认")
    r = delivery.handoff(app.store, job, "local", "标题", "简介", app.cfg.root)
    dest = app.cfg.root / "handoff" / job
    assert (dest / "a.main.mp4").read_bytes() == b"fake" and "转载自 C《T》" in (dest / "credits.txt").read_text()
    assert json.loads((dest / "meta.json").read_text())["title"] == "标题" and r["files"]
    assert delivery.state(app.store, job)["status"] == "handed_off"


def test_reject_records_reason_and_blocks_handoff(app: App) -> None:
    job = _awaiting(app)
    with pytest.raises(PolicyError):
        delivery.reject(app.store, job, "nonsense", "", [])
    delivery.reject(app.store, job, "boring", "没意思", ["main"])
    rej = json.loads((app.store.job_dir(job) / "rejections.json").read_text())
    assert rej["items"][0]["reason"] == "boring"
    with pytest.raises(PolicyError):
        delivery.handoff(app.store, job, "local", "", "", app.cfg.root)


def test_http_confirm_flow_blocks_agent_shortcut(http: Any, app: App) -> None:
    job = _awaiting(app)
    code, b, _ = http("POST", f"/v1/jobs/{job}/handoff", {"to": "local"})
    assert code == 400 and b["error"]["code"] == "not_confirmed"
    code, b, _ = http("POST", f"/v1/jobs/{job}/confirm", {})
    assert code == 400 and b["error"]["code"] == "quote_required"
    code, b, _ = http("POST", f"/v1/jobs/{job}/confirm", {"quote": "可以"})
    assert code == 200
    code, b, _ = http("POST", f"/v1/jobs/{job}/handoff", {"to": "local"})
    assert code == 200 and b["ok"]


def test_concurrent_run_saves_do_not_collide(app: App) -> None:
    """多个线程同时写同一个 run 记录（提交线程 + 执行线程）不能因为临时文件重名而丢失。"""
    job = app.store.create_job("t", {})["id"]
    run = {"id": "r-000000-bbbbbb", "job": job, "status": "queued"}
    errs: list[Exception] = []

    def hammer() -> None:
        try:
            for i in range(200):
                app.store.save_run(job, {**run, "n": i})
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=hammer) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and app.store.get_run(job, run["id"])["status"] == "queued"


def test_reject_specific_outputs_keeps_others_and_records_zones(app: App) -> None:
    job = app.store.create_job("t", {})["id"]
    d = app.store.job_dir(job)
    (d / "out").mkdir(exist_ok=True)
    for n in ("a", "b"):
        (d / "out" / f"{n}.mp4").write_bytes(b"x")
    delivery.mark_awaiting(app.store, job, [{"id": "c1", "final": "out/a.mp4", "range": [10.0, 50.0]}, {"id": "c2", "final": "out/b.mp4", "range": [100.0, 140.0]}], "s")
    with pytest.raises(PolicyError) as e:
        delivery.reject(app.store, job, "boring", "", ["nope"])
    assert e.value.code == "no_such_output"
    delivery.reject(app.store, job, "boring", "没意思", ["c2"])
    ctx = delivery.rejection_context(app.store, job)
    assert ctx["zones"] == [[100.0, 140.0]] and ctx["rounds"] == 1 and ctx["feedback"][0]["text"] == "没意思"
    assert [x["id"] for x in delivery.live(delivery.state(app.store, job))] == ["c1"]
    # 补做：新一轮只加新的，c1 带过去
    (d / "out" / "c.mp4").write_bytes(b"x")
    delivery.mark_awaiting(app.store, job, [{"id": "r2_1", "final": "out/c.mp4", "range": [200.0, 240.0]}], "s2", carry=True)
    st = delivery.state(app.store, job)
    assert [x["id"] for x in st["deliverables"]] == ["c1", "r2_1"] and st["status"] == "awaiting_confirmation"
    delivery.confirm(app.store, job, "可以")
    delivery.handoff(app.store, job, "local", "", "", app.cfg.root)
    assert sorted(p.name for p in (app.cfg.root / "handoff" / job).glob("*.mp4")) == ["a.mp4", "c.mp4"]  # 被拒的 b 不交付


def test_pass_rate_triggers_degraded_mode_below_half(app: App) -> None:
    root = app.cfg.root
    assert delivery.pass_rate(root)["degraded_mode"] is False
    for ev in ["confirmed", "rejected", "rejected", "rejected"]:
        delivery._metric(root, "j", ev, n=1)
    pr = delivery.pass_rate(root)
    assert pr["n"] == 4 and pr["rate"] == 0.25 and pr["degraded_mode"] is True
    for _ in range(6):
        delivery._metric(root, "j", "confirmed", n=1)
    assert delivery.pass_rate(root)["degraded_mode"] is False  # 最近 10 条 7/10


def test_ingest_helper_detects_truncated_video(tmp_path: Path) -> None:
    """下载下来视频流比音频短一大截（2026-10-31 实测的 AV1 坏文件）必须被发现。"""
    import subprocess as sp

    from autocut.worker import ingest_ytdlp as h

    good = tmp_path / "good.mp4"
    sp.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=8:s=160x90:r=10", "-f", "lavfi", "-i", "sine=d=8", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(good)], check=True)
    assert h.integrity_problem(good) is None
    bad = tmp_path / "bad.mp4"  # 视频 3s + 音频 8s
    sp.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=3:s=160x90:r=10", "-f", "lavfi", "-i", "sine=d=8", "-c:v", "libx264", "-c:a", "aac", str(bad)], check=True)
    assert "对不上" in (h.integrity_problem(bad) or "")


# ------------------------------------------------------------------ DSH 事件

from autocut.worker import events  # noqa: E402


def test_event_for_pipeline_results() -> None:
    ok = {"id": "r-1", "kind": "pipeline", "cmd": "cut", "status": "ok", "result": {"state": "awaiting_confirmation", "deliverables": [{}, {}], "summary": "剪了 2 分钟"}}
    e = events.event_for("job1", ok)
    assert e and e["kind"] == "autocut.ready" and e["subject"] == "job1" and e["key"] == "job1-r-1" and "2 条成片" in e["title"] and "剪了 2 分钟" in e["text"]
    bad = {"id": "r-2", "kind": "pipeline", "cmd": "clips", "status": "failed", "result": {"error": {"code": "none_passed", "message": "没有一条通过", "fix": "换素材"}}}
    f = events.event_for("job1", bad)
    assert f and f["kind"] == "autocut.failed" and "none_passed" in f["title"] and "换素材" in f["text"]
    na = events.event_for("job1", {"id": "r-5", "kind": "pipeline", "cmd": "commentary_prepare", "status": "ok", "result": {"state": "needs_agent", "needs": "script", "brief": {"huge": "x" * 50000}}})
    assert na and na["kind"] == "autocut.needs_agent" and "autocut-remote wait job1 r-5" in na["text"] and len(na["text"]) < 400 and "huge" not in na["text"]  # brief 不塞进事件，让 agent 自己取
    assert events.event_for("job1", {"id": "r-3", "kind": "autocut", "status": "ok"}) is None  # 单个子命令不发
    assert events.event_for("job1", {"id": "r-4", "kind": "pipeline", "status": "cancelled"}) is None
    for ev in (e, f):  # DSH 入口的字段限制：kind 以 source. 开头、key/subject ≤200、title ≤300、text ≤8000
        assert ev["kind"].startswith("autocut.") and len(ev["key"]) <= 200 and len(ev["title"]) <= 300 and len(ev["text"]) <= 8000


def test_emitter_posts_with_bearer_and_survives_failure(tmp_path: Path) -> None:
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got: list[tuple[str, dict]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers["Content-Length"])
            got.append((self.headers.get("Authorization", ""), _json.loads(self.rfile.read(n))))
            self.send_response(202)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sec = tmp_path / "w.env"
    sec.write_text(f"AUTOCUT_DSH_EVENT_TOKEN=tok123\nAUTOCUT_DSH_EVENT_URL=http://127.0.0.1:{srv.server_address[1]}/hooks/events\n")
    em = events.Emitter(WorkerConfig(root=tmp_path, secrets_file=str(sec)))
    em("j", {"id": "r-1", "kind": "pipeline", "cmd": "cut", "status": "failed", "result": {"error": {"code": "x", "message": "m", "fix": "f"}}})
    assert got and got[0][0] == "Bearer tok123" and got[0][1]["kind"] == "autocut.failed"
    srv.shutdown()
    em("j", {"id": "r-2", "kind": "pipeline", "cmd": "cut", "status": "failed", "result": {}})  # 服务挂了：只记日志，不抛
    assert not events.Emitter(WorkerConfig(root=tmp_path)).enabled  # 没配 token 就不发


# ------------------------------------------------------------------ 交给烤肉（假烤肉 API）


class _FakeKaorou:
    def __init__(self) -> None:
        import hashlib as _h
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.calls: list[tuple[str, str]] = []
        self.sessions: dict[str, dict] = {}
        self.bodies: dict[str, bytes] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else {}
                outer.calls.append(("POST", self.path))
                if self.path == "/external-uploads":
                    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, body["client_upload_id"]))
                    s = outer.sessions.setdefault(uid, {"upload_id": uid, "status": "awaiting_content", "request": body, "job_id": None})
                    return self._send(201, s)
                if self.path.endswith("/commit"):
                    uid = self.path.split("/")[2]
                    outer.sessions[uid].update(status="queued", job_id="job-42")
                    return self._send(200, outer.sessions[uid])
                self._send(404, {})

            def do_PUT(self):
                uid = self.path.split("/")[2]
                n = int(self.headers["Content-Length"])
                data = self.rfile.read(n)
                outer.calls.append(("PUT", self.path))
                outer.bodies[uid] = data
                if _h.sha256(data).hexdigest() != outer.sessions[uid]["request"]["sha256"]:
                    return self._send(422, {"detail": "mismatch"})
                outer.sessions[uid]["status"] = "ready"
                self._send(200, outer.sessions[uid])

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.srv.server_address[1]}"


def _confirmed_job(app: App, third: bool = True, url: str = "https://youtu.be/abc") -> str:
    job = app.store.create_job("t", {"third_party": third, "attribution": {"repost_label": True}})["id"]
    d = app.store.job_dir(job)
    (d / "out").mkdir(exist_ok=True)
    (d / "out" / "a.main.mp4").write_bytes(b"fakevideo" * 1000)
    ingest.register_source(app.store, job, {"kind": "url", "file": "src/a.mp4", "url": url, "title": "T", "channel": "C"})
    delivery.mark_awaiting(app.store, job, [{"id": "main", "final": "out/a.main.mp4", "preview": "out/p.mp4", "extras": [], "role": "main"}], "s")
    delivery.confirm(app.store, job, "可以发")
    return job


def test_handoff_to_kaorou_sequence_defaults_and_idempotency(app: App) -> None:
    fk = _FakeKaorou()
    job = _confirmed_job(app)
    r = delivery.handoff(app.store, job, "kaorou", "我的标题", "简介", app.cfg.root, kaorou={"tags": ["游戏", "解说"], "api": fk.api})
    assert [c[0] for c in fk.calls] == ["POST", "PUT", "POST"] and fk.calls[2][1].endswith("/commit")
    up = r["kaorou"][0]
    sess = next(iter(fk.sessions.values()))["request"]
    assert up["job_id"] == "job-42" and up["visibility"] == "self"  # 默认仅自己可见
    assert sess["copyright"] == 2 and sess["source_url"] == "https://youtu.be/abc" and sess["tid"] == 17 and "转载自 C《T》" in sess["description"]
    assert next(iter(fk.bodies.values())) == b"fakevideo" * 1000
    # 重试（同内容）：同一个会话，不重复上传
    n_put = sum(1 for c in fk.calls if c[0] == "PUT")
    delivery.handoff(app.store, job, "kaorou", "我的标题", "简介", app.cfg.root, kaorou={"tags": ["游戏", "解说"], "api": fk.api})
    assert sum(1 for c in fk.calls if c[0] == "PUT") == n_put and len(fk.sessions) == 1


def test_handoff_to_kaorou_gate_and_validation(app: App) -> None:
    fk = _FakeKaorou()
    job = app.store.create_job("t", {})["id"]
    delivery.mark_awaiting(app.store, job, [{"id": "main", "final": "out/a.mp4", "role": "main"}], "s")
    with pytest.raises(PolicyError) as e:  # 没确认：烤肉一次都不会被调用
        delivery.handoff(app.store, job, "kaorou", "t", "", app.cfg.root, kaorou={"tags": ["x"], "api": fk.api})
    assert e.value.code == "not_confirmed" and fk.calls == []
    j2 = _confirmed_job(app, third=True, url="")  # 转载却没有原链接
    with pytest.raises(PolicyError) as e:
        delivery.handoff(app.store, j2, "kaorou", "t", "", app.cfg.root, kaorou={"tags": ["x"], "api": fk.api})
    assert e.value.code == "source_url_required" and fk.calls == []
    j3 = _confirmed_job(app)
    for kw, code in (({"tags": []}, "tags_required"), ({"tags": ["x"], "visibility": "friends"}, "bad_visibility")):
        with pytest.raises(PolicyError) as e:
            delivery.handoff(app.store, j3, "kaorou", "t", "", app.cfg.root, kaorou={"api": fk.api, **kw})
        assert e.value.code == code
    with pytest.raises(PolicyError) as e:
        delivery.handoff(app.store, j3, "kaorou", "", "", app.cfg.root, kaorou={"tags": ["x"], "api": fk.api})
    assert e.value.code == "title_required" and fk.calls == []


def test_kaorou_unreachable_is_reported(app: App) -> None:
    job = _confirmed_job(app)
    with pytest.raises(PolicyError) as e:
        delivery.handoff(app.store, job, "kaorou", "t", "", app.cfg.root, kaorou={"tags": ["x"], "api": "http://127.0.0.1:9"})
    assert e.value.code == "kaorou_unreachable"


def test_preview_endpoint_resizes_live_previews(http: Any, app: App, tmp_path: Path) -> None:
    import subprocess as sp

    job = app.store.create_job("t", {})["id"]
    d = app.store.job_dir(job)
    (d / "out").mkdir(exist_ok=True)
    sp.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=20:s=640x360:r=25", "-f", "lavfi", "-i", "sine=d=20", "-c:v", "libx264", "-b:v", "1500k", "-c:a", "aac", str(d / "out" / "a.mp4")], check=True)
    delivery.mark_awaiting(app.store, job, [{"id": "main", "final": "out/a.mp4", "preview": "out/p.mp4", "role": "main"}], "s")
    code, b, _ = http("POST", f"/v1/jobs/{job}/preview", {"max_mib": 0.5})
    assert code == 200 and b["previews"][0]["bytes"] <= 0.5 * 1024 * 1024 + 2 * 2**20
    assert delivery.state(app.store, job)["deliverables"][0]["preview"].startswith("out/preview-5-")


def test_pipeline_start_is_idempotent_while_running(app: App) -> None:
    from autocut.worker.pipeline import start

    job = app.store.create_job("t", {})["id"]
    gate = threading.Event()

    def slow(ctx):
        gate.wait(10)
        return {"ok": True}

    r1 = start(app, job, "demo", {}, slow)
    r2 = start(app, job, "demo", {}, slow)  # agent 手快提交了两次
    assert r2["id"] == r1["id"] and r2.get("reused") is True
    r3 = start(app, job, "other", {}, slow)  # 不同名的流水线不受影响
    assert r3["id"] != r1["id"]
    gate.set()
    wait_done(app, job, r1["id"])
    r4 = start(app, job, "demo", {}, lambda c: {"ok": True})  # 跑完之后可以重新提交（例如缩短后重提稿子）
    assert r4["id"] != r1["id"]
