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
    for ok in ("cut", "render", "show", "check"):
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
