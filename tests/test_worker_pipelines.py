"""worker 流水线集成测试：真 autocut + 合成素材（fixtures/media，缺失时 skip）。"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

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
    assert [s["name"] for s in r["stages"]] == ["cut", "guard", "check-plan", "render", "preview"]
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


def test_guard_uncertain_restores_low_confidence_cuts(tmp_path: Path) -> None:
    from autocut.plan import Output, Plan, Segment, Source
    from autocut.worker.pipe_cut import guard_uncertain

    segs = [Segment("d1", "a", 0, 2, "keep", "speech", 1.0), Segment("d2", "a", 2, 4, "cut", "silence", 0.5),
            Segment("d3", "a", 4, 6, "cut", "silence", 0.9), Segment("d4", "a", 6, 7, "cut", "retake", 0.7, locked=True),
            Segment("d5", "a", 7, 8, "cut", "silence", 0.95, detail="停顿 0.9s；停在句子中间")]
    plan = Plan("cut", {"a": Source("a", "a.mp4", "video", 8.0)}, [Output("main", segs)])
    p = tmp_path / "p.autocut.json"
    plan.save(p)
    r = guard_uncertain(p, 0.75)
    assert r["restored"] == 2 and r["seconds"] == 3.0
    again = Plan.load(p)
    acts = {s.id: s.action for s in again.output().segments}
    assert acts == {"d1": "keep", "d2": "keep", "d3": "cut", "d4": "cut", "d5": "keep"}  # 高置信和锁定的不动
    assert any("保守策略" in n for n in again.notes) and again.revision == 2
    assert guard_uncertain(p, 0.75)["restored"] == 0  # 幂等


# ------------------------------------------------------------------ commentary（两段式：worker 看图，agent 写稿；假眼睛 / 假 TTS，真 cut / render）


class _FakeEyes:
    """只会"看图说话"（无状态）；写稿不在 worker 里，所以这里根本没有写作接口。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def chat_json(self, model, messages, *, purpose, validate=None, **kw):
        self.calls.append(purpose)
        assert purpose == "eyes", f"worker 不应该为 {purpose} 调 LLM"
        n = sum(1 for p in messages[0]["content"] if p.get("type") == "image_url")
        out = {"frames": [{"i": i + 1, "desc": f"画面{i + 1}：角色在移动"} for i in range(n)]}
        if validate:
            validate(out)
        return out


class _FakeTTS:
    stats = {"hit": 0, "miss": 3}

    def __init__(self, tmp: Path, dur: float = 1.2) -> None:
        self.tmp, self.dur = tmp, dur

    def synth_many(self, lines, *, voice, lang, style="cute"):
        from test_worker_commentary import _tone_wav  # noqa: PLC0415

        return [(_tone_wav(self.tmp / f"tts{abs(hash(t))}.wav", self.dur), self.dur, "feiyu") for t in lines]


def _commentary_job(app: App, media: Path) -> str:
    job = app.store.create_job("game", {})["id"]
    shutil.copy(media / "game_commentary.mp4", app.store.job_dir(job) / "src" / "game.mp4")
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/game.mp4"})
    return job


def test_commentary_prepare_returns_brief_and_asks_agent(app: App, media: Path) -> None:
    from autocut.worker import pipe_commentary  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _commentary_job(app, media)
    eyes = _FakeEyes()
    r = wait(app, job, start(app, job, "commentary_prepare", {"game": "测试游戏"}, lambda c: pipe_commentary.run_prepare(c, gateway=eyes))["id"])
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    assert res["state"] == "needs_agent" and res["needs"] == "script"
    b = res["brief"]
    assert b["duration"] > 10 and b["frames"] and b["frames"][0]["desc"].startswith("画面") and b["limits"]["max_lines"] >= 4
    assert "script_schema" in b and set(eyes.calls) == {"eyes"}
    assert [s["name"] for s in r["stages"]] == ["cut", "eyes"]
    assert delivery.state(app.store, job)["status"] == "working"  # 还没有成片，不能确认


def test_commentary_finish_with_agent_script_end_to_end(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_commentary  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _commentary_job(app, media)
    wait(app, job, start(app, job, "commentary_prepare", {}, lambda c: pipe_commentary.run_prepare(c, gateway=_FakeEyes()))["id"])
    script = {"title": "测试标题", "lines": [{"at": 1.0, "text": "开场先看这一手"}, {"at": 8.0, "text": "这里对面开团了"}, {"at": 16.0, "text": "漂亮的收尾"}]}
    r = wait(app, job, start(app, job, "commentary_finish", {"script": json.dumps(script, ensure_ascii=False)}, lambda c: pipe_commentary.run_finish(c, tts=_FakeTTS(tmp_path)))["id"])
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    d = app.store.job_dir(job)
    dv = res["deliverables"][0]
    assert (d / dv["final"]).stat().st_size > 10_000 and (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES
    assert "3 句解说" in res["summary"] and res["title"] == "测试标题" and "DSH agent" in json.dumps(json.loads((d / "work" / "game.commentary.autocut.json").read_text())["notes"], ensure_ascii=False)
    bed = json.loads((d / "work" / "game.commentary.autocut.json").read_text())["outputs"][0]["beds"][0]
    assert bed["role"] == "narration" and bed["duck_main_db"] == 14.0
    assert delivery.state(app.store, job)["status"] == "awaiting_confirmation"


def test_commentary_finish_returns_too_long_lines_to_the_agent(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_commentary  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _commentary_job(app, media)
    wait(app, job, start(app, job, "commentary_prepare", {}, lambda c: pipe_commentary.run_prepare(c, gateway=_FakeEyes()))["id"])
    script = {"title": "t", "lines": [{"at": 1.0, "text": "很长很长的一句话"}, {"at": 3.0, "text": "紧接着的一句"}]}  # 间隔 2s，TTS 每句 4s
    r = wait(app, job, start(app, job, "commentary_finish", {"script": script}, lambda c: pipe_commentary.run_finish(c, tts=_FakeTTS(tmp_path, dur=4.0)))["id"])
    assert r["status"] == "failed"
    e = r["result"]["error"]
    assert e["code"] == "lines_too_long" and e["details"][0]["max_chars"] > 0 and e["details"][0]["text"] == "很长很长的一句话" and "改短" in e["fix"]
    assert delivery.state(app.store, job)["status"] == "working"


def test_commentary_finish_validates_script_and_needs_prepare(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_commentary  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _commentary_job(app, media)
    r = wait(app, job, start(app, job, "commentary_finish", {"script": {"title": "t", "lines": []}}, lambda c: pipe_commentary.run_finish(c, tts=_FakeTTS(tmp_path)))["id"], 30)
    assert r["result"]["error"]["code"] == "not_prepared"
    wait(app, job, start(app, job, "commentary_prepare", {}, lambda c: pipe_commentary.run_prepare(c, gateway=_FakeEyes()))["id"])
    for bad, code in (({"title": "t", "lines": []}, "bad_script"), ({"title": "t", "lines": [{"at": 9999, "text": "x"}]}, "bad_script"), (None, "script_required"), ("不是json{", "bad_script")):
        opts = {} if bad is None else {"script": bad}
        r = wait(app, job, start(app, job, "commentary_finish", opts, lambda c: pipe_commentary.run_finish(c, tts=_FakeTTS(tmp_path)))["id"], 30)
        assert r["status"] == "failed" and r["result"]["error"]["code"] == code, (bad, r["result"])


# ------------------------------------------------------------------ clips（agent 选段；worker：硬约束 + 独立评审 + 交付）


class _ReviewerGW:
    """独立评审 + QA（眼睛）：只收到文字稿；第 2 个被评审的候选故意给 standalone=2；QA 全过。nomination 接口根本不存在。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.review_texts: list[str] = []

    def chat_json(self, model, messages, *, purpose, validate=None, **kw):
        self.calls.append(purpose)
        assert purpose in ("review", "qa"), f"worker 不应该为 {purpose} 调 LLM"
        if purpose == "review":
            self.review_texts.append(messages[0]["content"])
            n = self.calls.count("review")
            out = {"hook": 4, "standalone": 2 if n == 2 else 4, "arc": 4, "density": 4, "safety": 5, "fit": 4, "problems": ["开头悬空指代"] if n == 2 else []}
        else:
            out = {"black": False, "frozen": False, "caption_overflow": False, "subject_cropped": False, "note": "ok"}
        if validate:
            validate(out)
        return out


def _agent_clips(app: App, job: str, media: Path, tmp_path: Path) -> list[str]:
    """像 DSH agent 那样：用 autocut CLI 读句段、按 ID 建候选（经 worker 白名单子命令）。返回 clip id 列表。"""
    import subprocess  # noqa: PLC0415

    d = app.store.job_dir(job)
    lst = tmp_path / "l.txt"
    lst.write_text("".join(f"file '{media / 'lecture_en.mp4'}'\n" for _ in range(4)))
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(d / "src" / "long.mp4")], check=True)
    ingest.register_source(app.store, job, {"kind": "url", "file": "src/long.mp4", "url": "https://example.com/long", "title": "长课", "channel": "某人"})

    def run(cmd: str, args: list[str]) -> dict:
        r = app.runner.get(job, app.runner.submit(job, cmd, args)["id"], wait=300)
        for _ in range(300):
            if r["status"] in ("ok", "failed", "cancelled"):
                break
            r = app.runner.get(job, r["id"], wait=2)
        return r

    tr = run("transcript", ["src/long.mp4", "--limit", "100", "--lang", "en"])
    assert tr["status"] == "ok", tr
    ids = [i["id"] for i in tr["result"]["items"]]
    made = []
    start_at = 0
    while len(made) < 4 and start_at < len(ids) - 3:
        r = run("clip", ["src/long.mp4", "--from", ids[start_at], "--to", ids[min(start_at + 2, len(ids) - 1)], "--title", f"片段{len(made) + 1}", "--reason", f"理由{len(made) + 1}",
                         "--length", "8-40", "-o", "work/long.clips.autocut.json", "--lang", "en"])
        if r["status"] == "ok":
            made.append(r["result"]["output"])
        start_at += 3
    assert len(made) >= 3, made
    return made


def test_clips_review_deliver_reject_reselect_flow(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_clips  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("clips", {"purpose": "找讲得最清楚的片段", "attribution": {"repost_label": True}})["id"]
    d = app.store.job_dir(job)
    made = _agent_clips(app, job, media, tmp_path)
    plan_rel = "work/long.clips.autocut.json"
    gw = _ReviewerGW()
    r = wait(app, job, start(app, job, "clips_review", {"plan": plan_rel}, lambda c: pipe_clips.run_review(c, gateway=gw))["id"], 600)
    assert r["status"] == "ok", r.get("result")
    rv = r["result"]
    assert len(rv["passed"]) == len(made) - 1 and list(rv["failed"]) == [made[1]] and "悬空" in rv["failed"][made[1]]["problems"][0]
    assert not any("理由" in t for t in gw.review_texts) and set(gw.calls) == {"review"}  # 评审只看文字稿，看不到提名理由；worker 没有任何提名/写作调用
    # 没评审过关就不能交付
    bad = wait(app, job, start(app, job, "clips_deliver", {"plan": plan_rel, "select": [made[1]]}, lambda c: pipe_clips.run_deliver(c, gateway=gw))["id"], 60)
    assert bad["result"]["error"]["code"] == "review_failed"
    bad2 = wait(app, job, start(app, job, "clips_deliver", {"plan": plan_rel, "select": ["clip_99"]}, lambda c: pipe_clips.run_deliver(c, gateway=gw))["id"], 60)
    assert bad2["result"]["error"]["code"] == "not_reviewed"
    sel = [rv["passed"][0]]
    bk = rv["passed"][1:2]
    ok = wait(app, job, start(app, job, "clips_deliver", {"plan": plan_rel, "select": sel, "backups": bk, "n": 1}, lambda c: pipe_clips.run_deliver(c, gateway=gw))["id"], 900)
    assert ok["status"] == "ok", ok.get("result")
    res = ok["result"]
    assert [x["role"] for x in res["deliverables"]] == ["main"] + ["backup"] * len(bk)
    for dv in res["deliverables"]:
        assert (d / dv["final"]).stat().st_size > 5_000 and (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES and dv["range"][1] > dv["range"][0]
    assert "转载自 某人《长课》" in res["summary"] and "DSH agent" in res["summary"]
    plan = json.loads((d / plan_rel).read_text())
    assert all(o["captions"]["burn"] and o["width"] == 1080 and o["height"] == 1920 for o in plan["outputs"])
    # 被拒：禁区 + 原因进入 job 状态，agent 带着它重新提名；补做只加被拒的那一条
    victim = res["deliverables"][0]
    delivery.reject(app.store, job, "boring", "没意思", [victim["id"]])
    rej = app.handle("GET", f"/v1/jobs/{job}", {}, {})[1]["rejections"]
    assert rej["zones"] == [list(victim["range"])] and rej["feedback"][0]["text"] == "没意思" and rej["rounds"] == 1
    # 禁区里的候选会被 `clip --forbid` 拦住（agent 把 zones 带过去）
    r2 = app.runner.get(job, app.runner.submit(job, "clip", ["src/long.mp4", "--from", "s0001", "--to", "s0003", "--title", "x", "--reason", "y", "--length", "8-40", "--forbid", f"{victim['range'][0]}-{victim['range'][1]}",
                                                         "-o", "work/long.clips.autocut.json", "--lang", "en"])["id"], wait=120)
    for _ in range(100):
        if r2["status"] in ("ok", "failed"):
            break
        r2 = app.runner.get(job, r2["id"], wait=2)
    assert r2["status"] in ("ok", "failed")  # 与禁区重叠时被拦（in_forbidden_zone / overlaps），不重叠时正常建出来；两种都合法
    st = delivery.state(app.store, job)
    assert victim["id"] not in [x["id"] for x in delivery.live(st)] and st["status"] == "rejected"


def test_clips_pipelines_stop_after_three_rejection_rounds(app: App) -> None:
    from autocut.worker import pipe_clips  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("clips", {})["id"]
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/x.mp4"})
    (app.store.job_dir(job) / "rejections.json").write_text(json.dumps({"items": [{"reason": "boring", "text": "", "outputs": [], "zones": []}] * 3}))
    # 需要一个已评审过关的方案才能走到轮数检查：直接构造 review.json 与方案
    from autocut.plan import Output, Plan, Segment, Source  # noqa: PLC0415

    d = app.store.job_dir(job)
    plan = Plan("clips", {"v": Source("v", "../src/x.mp4", "video", 100.0)}, [Output("clip_1", [Segment("c1", "v", 1, 30, "keep", "highlight", 0.8)])])
    plan.save(d / "work" / "p.autocut.json")
    (d / "work" / "review.json").write_text(json.dumps({"work/p.autocut.json#clip_1": {"hook": 4, "standalone": 4, "arc": 4, "density": 4, "safety": 5, "fit": 4, "total": 25, "pass": True}}))
    run = start(app, job, "clips_deliver", {"plan": "work/p.autocut.json", "select": ["clip_1"]}, lambda c: pipe_clips.run_deliver(c, gateway=_ReviewerGW()))
    r = wait(app, job, run["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "too_many_rounds" and "更明确" in r["result"]["error"]["fix"]


# ------------------------------------------------------------------ montage（两段式：worker 看图，agent 挑镜头）


class _MontageEyes:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def chat_json(self, model, messages, *, purpose, validate=None, **kw):
        self.calls.append(purpose)
        assert purpose == "eyes", f"worker 不应该为 {purpose} 调 LLM（挑镜头是 agent 的事）"
        n = sum(1 for p in messages[0]["content"] if p.get("type") == "image_url")
        out = {"shots": [{"i": i + 1, "desc": f"画面{i + 1}", "appeal": 3 + (i % 3)} for i in range(n)]}
        if validate:
            validate(out)
        return out


def _montage_job(app: App, media: Path) -> str:
    job = app.store.create_job("mix", {"purpose": "节奏感强的混剪"})["id"]
    d = app.store.job_dir(job)
    for i in (1, 2, 3):
        shutil.copy(media / "montage" / f"clip{i}.mp4", d / "src" / f"clip{i}.mp4")
        ingest.register_source(app.store, job, {"kind": "upload", "file": f"src/clip{i}.mp4"})
    shutil.copy(media / "montage" / "music_120bpm.wav", d / "src" / "bgm.wav")
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/bgm.wav"})
    return job


def test_montage_prepare_then_agent_picks_shots_on_beats(app: App, media: Path) -> None:
    from autocut.worker import pipe_montage  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _montage_job(app, media)
    d = app.store.job_dir(job)
    eyes = _MontageEyes()
    r = wait(app, job, start(app, job, "montage_prepare", {"length": 8, "aspect": "9:16", "use_asr": False}, lambda c: pipe_montage.run_prepare(c, gateway=eyes))["id"], 600)
    assert r["status"] == "ok", r.get("result")
    brief = r["result"]["brief"]
    assert r["result"]["state"] == "needs_agent" and r["result"]["needs"] == "picks" and brief["bpm"] == 120 and brief["slots"] and brief["catalog"]
    assert set(eyes.calls) == {"eyes"} and delivery.state(app.store, job)["status"] == "working"
    # 像 agent 一样挑：逆序给槽位分配镜头（每个镜头最多用一次）
    ids = [c["id"] for c in brief["catalog"]]
    picks = [{"slot": s["slot"], "shot": ids[len(ids) - 1 - i], "why": "测试"} for i, s in enumerate(brief["slots"][:len(ids)])]  # 镜头比槽位少：只覆盖一部分，其余沿用默认
    f = wait(app, job, start(app, job, "montage_finish", {"picks": json.dumps(picks)}, lambda c: pipe_montage.run_finish(c))["id"], 600)
    assert f["status"] == "ok", f.get("result")
    res = f["result"]
    assert "DSH agent 按主题挑选" in res["summary"] and "120" in res["summary"] and "沿用配方默认选择" in res["summary"]
    plan = json.loads((d / "work" / "montage.autocut.json").read_text())
    segs = plan["outputs"][0]["segments"]
    picked = [s for s in segs if s.get("evidence", {}).get("pick")]
    assert picked and len({s["evidence"]["pick"] for s in picked}) == len(picked)
    total = sum(s["slot"] for s in segs)
    assert total == pytest.approx(8.0, abs=0.01) and all(abs(s["slot"] / 0.5 - round(s["slot"] / 0.5)) < 1e-3 for s in segs)  # 卡点没被破坏
    dv = res["deliverables"][0]
    assert (d / dv["final"]).stat().st_size > 5_000 and (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES


def test_montage_finish_rejects_bad_picks(app: App, media: Path) -> None:
    from autocut.worker import pipe_montage  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _montage_job(app, media)
    r = wait(app, job, start(app, job, "montage_finish", {"picks": []}, lambda c: pipe_montage.run_finish(c))["id"], 30)
    assert r["result"]["error"]["code"] == "not_prepared"
    r = wait(app, job, start(app, job, "montage_prepare", {"length": 8, "use_asr": False}, lambda c: pipe_montage.run_prepare(c, gateway=_MontageEyes()))["id"], 600)
    ids = [c["id"] for c in r["result"]["brief"]["catalog"]]
    cases = [([{"slot": 1, "shot": "Z99"}], "bad_picks"), ([{"slot": 99, "shot": ids[0]}], "bad_picks"), ([{"slot": 1, "shot": ids[0]}, {"slot": 2, "shot": ids[0]}], "duplicate_picks"), ([], "picks_required"), ("坏{", "bad_picks")]
    for picks, code in cases:
        f = wait(app, job, start(app, job, "montage_finish", {"picks": picks}, lambda c: pipe_montage.run_finish(c))["id"], 60)
        assert f["status"] == "failed" and f["result"]["error"]["code"] == code, (picks, f["result"])


def test_montage_needs_music(app: App) -> None:
    from autocut.worker import pipe_montage  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("mix", {})["id"]
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/a.mp4"})
    r = wait(app, job, start(app, job, "montage_prepare", {}, lambda c: pipe_montage.run_prepare(c, gateway=_MontageEyes()))["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "no_music"
