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


def _cut_job(app: App, media: Path, name: str = "talk_zh.mp4") -> str:
    job = app.store.create_job("cut", {"attribution": {"repost_label": True}})["id"]
    shutil.copy(media / name, app.store.job_dir(job) / "src" / name)
    ingest.register_source(app.store, job, {"kind": "url", "file": f"src/{name}", "url": "https://example.com/v", "title": "测试", "channel": "某人"})
    return job


def test_cut_prepare_gives_proposal_brief_and_asks_agent(app: App, media: Path) -> None:
    job = _cut_job(app, media)
    r = wait(app, job, app.pipelines["cut_prepare"](app, job, {})["id"])
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    b = res["brief"]
    assert res["state"] == "needs_agent" and res["needs"] == "cut_decisions" and b["candidates"] and "keep" in b["how"]
    c0 = b["candidates"][0]
    assert {"id", "at", "dur", "reason", "conf", "detail", "before", "after"} <= set(c0)
    assert delivery.state(app.store, job)["status"] == "working"  # 提案不是成片，没人拍板前不渲染


def test_cut_finish_applies_agent_decisions_and_rules_validate(app: App, media: Path) -> None:
    job = _cut_job(app, media)
    pr = wait(app, job, app.pipelines["cut_prepare"](app, job, {})["id"])
    cands = pr["result"]["brief"]["candidates"]
    restore = [cands[0]["id"]]  # agent 决定恢复第一个候选
    r = wait(app, job, app.pipelines["cut_finish"](app, job, {"keep": json.dumps(restore), "cut": []})["id"])
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    d = app.store.job_dir(job)
    dv = res["deliverables"][0]
    assert (d / dv["final"]).stat().st_size > 10_000 and (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES
    assert "DSH agent 做出" in res["summary"] and "转载自 某人《测试》" in res["summary"]
    plan = json.loads((d / "work" / "talk_zh.autocut.json").read_text())
    seg = next(s for s in plan["outputs"][0]["segments"] if s["id"] == restore[0])
    assert seg["action"] == "keep" and "agent 决定保留" in seg["detail"]
    assert delivery.state(app.store, job)["status"] == "awaiting_confirmation"
    assert [s["name"] for s in r["stages"]] == ["validate", "check-plan", "render", "preview"]


def test_cut_finish_rejects_unknown_ids_locked_and_over_cutting(app: App, media: Path) -> None:
    job = _cut_job(app, media)
    r0 = wait(app, job, app.pipelines["cut_finish"](app, job, {})["id"], 30)
    assert r0["result"]["error"]["code"] == "not_prepared"
    pr = wait(app, job, app.pipelines["cut_prepare"](app, job, {})["id"])
    r = wait(app, job, app.pipelines["cut_finish"](app, job, {"keep": ["d99999"]})["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "unknown_segment" and "不要编" in r["result"]["error"]["fix"]
    r = wait(app, job, app.pipelines["cut_finish"](app, job, {"keep": "坏{"})["id"], 30)
    assert r["result"]["error"]["code"] == "bad_decisions"
    plan = json.loads((app.store.job_dir(job) / "work" / "talk_zh.autocut.json").read_text())
    speech = [s["id"] for s in plan["outputs"][0]["segments"] if s["action"] == "keep"]
    r = wait(app, job, app.pipelines["cut_finish"](app, job, {"cut": speech})["id"], 30)  # 把所有说话都剪掉
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "too_much_removed"


def test_cut_finish_restores_mid_sentence_cuts_by_rule(tmp_path: Path) -> None:
    from autocut.plan import Output, Plan, Segment, Source
    from autocut.worker.pipe_cut import guard_mid_sentence

    segs = [Segment("d1", "a", 0, 2, "keep", "speech", 1.0), Segment("d2", "a", 2, 4, "cut", "silence", 0.99, detail="停顿 2s；停在句子中间"),
            Segment("d3", "a", 4, 6, "cut", "silence", 0.5, detail="停顿 2s"), Segment("d4", "a", 6, 8, "cut", "silence", 0.9, detail="停在句子中间", locked=True)]
    plan = Plan("cut", {"a": Source("a", "a.mp4", "video", 8.0)}, [Output("main", segs)])
    p = tmp_path / "p.autocut.json"
    plan.save(p)
    r = guard_mid_sentence(p)
    acts = {s.id: s.action for s in Plan.load(p).output().segments}
    assert r["restored"] == 1 and acts == {"d1": "keep", "d2": "keep", "d3": "cut", "d4": "cut"}  # 规则只管"停在句子中间"；把握度低的由 agent 决定，锁定的不动
    assert guard_mid_sentence(p)["restored"] == 0


def test_cut_prepare_without_source_fails_with_fix(app: App) -> None:
    job = app.store.create_job("cut", {})["id"]
    r = wait(app, job, app.pipelines["cut_prepare"](app, job, {})["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "no_source" and r["result"]["error"]["fix"]



# ------------------------------------------------------------------ commentary（两段式：worker 看图，agent 写稿；假眼睛 / 假 TTS，真 cut / render）


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
    r = wait(app, job, start(app, job, "commentary_prepare", {"game": "测试游戏"}, lambda c: pipe_commentary.run_prepare(c))["id"])
    assert r["status"] == "ok", r.get("result")
    res = r["result"]
    assert res["state"] == "needs_agent" and res["needs"] == "script"
    b = res["brief"]
    assert b["duration"] > 10 and b["sheets"] and b["limits"]["max_lines"] >= 4 and "script_schema" in b and "frames" not in b
    sh = b["sheets"][0]
    assert (app.store.job_dir(job) / sh["path"]).stat().st_size > 2000 and sh["cells"][0]["label"].startswith("1 ")  # 关键帧拼图：带编号和时间标签，由 DSH 自己看
    names = [s["name"] for s in r["stages"]]
    assert names[0] == "cut" and names[-1] == "eyes" and "render" in names and "healed" in res  # 去停顿后先渲染核验、把落在说话中间的接缝处的剪法改回保留，再取关键帧
    assert delivery.state(app.store, job)["status"] == "working"  # 还没有成片，不能确认


def test_commentary_finish_with_agent_script_end_to_end(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_commentary  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _commentary_job(app, media)
    wait(app, job, start(app, job, "commentary_prepare", {}, lambda c: pipe_commentary.run_prepare(c))["id"])
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
    wait(app, job, start(app, job, "commentary_prepare", {}, lambda c: pipe_commentary.run_prepare(c))["id"])
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
    wait(app, job, start(app, job, "commentary_prepare", {}, lambda c: pipe_commentary.run_prepare(c))["id"])
    for bad, code in (({"title": "t", "lines": []}, "bad_script"), ({"title": "t", "lines": [{"at": 9999, "text": "x"}]}, "bad_script"), (None, "script_required"), ("不是json{", "bad_script")):
        opts = {} if bad is None else {"script": bad}
        r = wait(app, job, start(app, job, "commentary_finish", opts, lambda c: pipe_commentary.run_finish(c, tts=_FakeTTS(tmp_path)))["id"], 30)
        assert r["status"] == "failed" and r["result"]["error"]["code"] == code, (bad, r["result"])


# ------------------------------------------------------------------ clips（agent 选段；worker：硬约束 + 独立评审 + 交付）


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


def test_clips_deliver_reject_reselect_flow(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_clips  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("clips", {"purpose": "找讲得最清楚的片段", "attribution": {"repost_label": True}})["id"]
    d = app.store.job_dir(job)
    made = _agent_clips(app, job, media, tmp_path)
    plan_rel = "work/long.clips.autocut.json"
    bad = wait(app, job, start(app, job, "clips_deliver", {"plan": plan_rel, "select": ["clip_99"]}, lambda c: pipe_clips.run_deliver(c))["id"], 60)
    assert bad["result"]["error"]["code"] == "unknown_output"
    sel, bk = [made[0]], made[1:2]
    ok = wait(app, job, start(app, job, "clips_deliver", {"plan": plan_rel, "select": sel, "backups": bk, "n": 1}, lambda c: pipe_clips.run_deliver(c))["id"], 900)
    assert ok["status"] == "ok", ok.get("result")
    res = ok["result"]
    assert [x["role"] for x in res["deliverables"]] == ["main"] + ["backup"] * len(bk)
    for dv in res["deliverables"]:
        assert (d / dv["final"]).stat().st_size > 5_000 and (d / dv["preview"]).stat().st_size <= delivery.PREVIEW_MAX_BYTES and dv["range"][1] > dv["range"][0]
    assert "转载自 某人《长课》" in res["summary"] and "DSH agent" in res["summary"] and "独立评审" not in res["summary"]
    plan = json.loads((d / plan_rel).read_text())
    assert all(o["captions"]["burn"] and o["width"] == 1080 and o["height"] == 1920 for o in plan["outputs"])
    victim = res["deliverables"][0]
    delivery.reject(app.store, job, "boring", "没意思", [victim["id"]])
    rej = app.handle("GET", f"/v1/jobs/{job}", {}, {})[1]["rejections"]
    assert rej["zones"] == [list(victim["range"])] and rej["feedback"][0]["text"] == "没意思" and rej["rounds"] == 1
    st = delivery.state(app.store, job)
    assert victim["id"] not in [x["id"] for x in delivery.live(st)] and st["status"] == "rejected"


def test_clips_deliver_blocks_sensitive_words(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_clips  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("clips", {})["id"]
    made = _agent_clips(app, job, media, tmp_path)
    r = wait(app, job, start(app, job, "clips_deliver", {"plan": "work/long.clips.autocut.json", "select": made[:1], "banned_words": ["the"]}, lambda c: pipe_clips.run_deliver(c))["id"], 120)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "sensitive_word"  # 命中敏感词直接拒绝，不渲染


def test_clips_pipelines_stop_after_three_rejection_rounds(app: App) -> None:
    from autocut.worker import pipe_clips  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = app.store.create_job("clips", {})["id"]
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/x.mp4"})
    (app.store.job_dir(job) / "rejections.json").write_text(json.dumps({"items": [{"reason": "boring", "text": "", "outputs": [], "zones": []}] * 3}))
    from autocut.plan import Output, Plan, Segment, Source  # noqa: PLC0415

    d = app.store.job_dir(job)
    plan = Plan("clips", {"v": Source("v", "../src/x.mp4", "video", 100.0)}, [Output("clip_1", [Segment("c1", "v", 1, 30, "keep", "highlight", 0.8)])])
    plan.save(d / "work" / "p.autocut.json")
    run = start(app, job, "clips_deliver", {"plan": "work/p.autocut.json", "select": ["clip_1"]}, lambda c: pipe_clips.run_deliver(c))
    r = wait(app, job, run["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "too_many_rounds" and "更明确" in r["result"]["error"]["fix"]


# ------------------------------------------------------------------ montage（两段式：worker 看图，agent 挑镜头）


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
    r = wait(app, job, start(app, job, "montage_prepare", {"length": 8, "aspect": "9:16", "use_asr": False}, lambda c: pipe_montage.run_prepare(c))["id"], 600)
    assert r["status"] == "ok", r.get("result")
    brief = r["result"]["brief"]
    assert r["result"]["state"] == "needs_agent" and r["result"]["needs"] == "picks" and brief["bpm"] == 120 and brief["slots"] and brief["catalog"]
    assert brief["sheets"] and (d / brief["sheets"][0]["path"]).stat().st_size > 2000 and "desc" not in brief["catalog"][0] and delivery.state(app.store, job)["status"] == "working"
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
    r = wait(app, job, start(app, job, "montage_prepare", {"length": 8, "use_asr": False}, lambda c: pipe_montage.run_prepare(c))["id"], 600)
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
    r = wait(app, job, start(app, job, "montage_prepare", {}, lambda c: pipe_montage.run_prepare(c))["id"], 30)
    assert r["status"] == "failed" and r["result"]["error"]["code"] == "no_music"


# ------------------------------------------------------------------ edl（agent 写剪辑计划，worker 校验后执行）


def _edl_job(app: App, media: Path) -> str:
    job = app.store.create_job("edl", {})["id"]
    d = app.store.job_dir(job)
    shutil.copy(media / "stream_vod.mp4", d / "src" / "vod.mp4")
    ingest.register_source(app.store, job, {"kind": "upload", "file": "src/vod.mp4"})
    return job


def test_edl_finish_executes_agent_plan_with_timelapse_and_narration(app: App, media: Path, tmp_path: Path) -> None:
    from autocut.worker import pipe_edl  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _edl_job(app, media)
    edl = {"title": "测试", "segments": [{"source": "src/vod.mp4", "start": 0, "end": 12, "speed": 1, "note": "开头"}, {"source": "src/vod.mp4", "start": 20, "end": 140, "speed": 20, "note": "延时"},
                                         {"source": "src/vod.mp4", "start": 140, "end": 148, "speed": 2, "note": "收尾"}],
           "lines": [{"at": 1.0, "text": "这是开头"}, {"at": 14.0, "text": "接下来快进"}]}
    r = wait(app, job, start(app, job, "edl_finish", {"edl": json.dumps(edl)}, lambda c: pipe_edl.run_finish(c, tts=_FakeTTS(tmp_path)))["id"], 900)
    assert r["status"] == "ok", r.get("result")
    d = app.store.job_dir(job)
    plan = json.loads((d / "work" / "edl.commentary.autocut.json").read_text())
    segs = plan["outputs"][0]["segments"]
    assert len(segs) == 3 and segs[1]["reason"] == "shot" and "延时摄影" in segs[1]["detail"] and segs[1]["mute"] is True  # 20 倍速 → 延时摄影中间片段
    tl = list((d / "work" / "edl-clips").glob("tl002-*.mp4"))
    assert tl and tl[0].stat().st_size > 1000 and abs(segs[1]["end"] - 6.0) < 0.4  # 120s 的 20 倍速 = 6s：成片时长恰好是 区间/倍速
    dv = r["result"]["deliverables"][0]
    assert (d / dv["final"]).stat().st_size > 5_000 and "2 句解说" in r["result"]["summary"] and "DSH agent" in r["result"]["summary"]


def test_edl_validation_treats_agent_output_as_untrusted(app: App, media: Path) -> None:
    from autocut.worker import pipe_edl  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _edl_job(app, media)
    seg = {"source": "src/vod.mp4", "start": 0, "end": 10}
    cases = [({"segments": []}, "bad_edl"), ({"segments": [{**seg, "end": 99999}]}, "bad_edl"), ({"segments": [{**seg, "source": "/etc/passwd"}]}, "bad_edl"),
             ({"segments": [{**seg, "speed": 1000}]}, "bad_edl"), ({"segments": [{**seg, "speed": 6}]}, "bad_edl"), ({"segments": [{"source": "src/vod.mp4", "start": 5, "end": 3}]}, "bad_edl"), ({"segments": [seg] * 301}, "too_many_segments"),
             ({"segments": [{**seg, "end": 100}], "title": "x"}, "bad_length"), ("坏{", "bad_edl")]
    for edl, code in cases:
        r = wait(app, job, start(app, job, "edl_finish", {"edl": edl, "max_seconds": 60}, lambda c: pipe_edl.run_finish(c))["id"], 60)
        assert r["status"] == "failed" and r["result"]["error"]["code"] == code, (edl, r["result"])
    r = wait(app, job, start(app, job, "edl_finish", {"edl": {"segments": [{**seg, "source": "../x.mp4"}]}}, lambda c: pipe_edl.run_finish(c))["id"], 60)
    assert r["result"]["error"]["code"] == "bad_edl" and "details" in r["result"]["error"]


def test_frame_endpoint_and_inbox_link(app: App, media: Path) -> None:
    from autocut.worker import policy  # noqa: PLC0415

    job = _edl_job(app, media)
    code, b = app.handle("POST", f"/v1/jobs/{job}/frame", {}, {"source": "src/vod.mp4", "t": 10, "width": 480})
    assert b["ok"] and (app.store.job_dir(job) / b["path"]).stat().st_size > 1000
    for bad in ({"source": "/etc/hosts", "t": 1}, {"source": "src/vod.mp4", "t": -1}):
        with pytest.raises(policy.PolicyError):
            app.handle("POST", f"/v1/jobs/{job}/frame", {}, bad)
    code, sh = app.handle("POST", f"/v1/jobs/{job}/sheet", {}, {"items": [{"source": "src/vod.mp4", "t": 5}, {"source": "src/vod.mp4", "t": 60}, {"source": "src/vod.mp4", "t": 120}], "cols": 3})
    assert sh["ok"] and (app.store.job_dir(job) / sh["path"]).stat().st_size > 2000 and len(sh["cells"]) == 3
    for bad in ({"items": []}, {"items": [{"source": "/etc/hosts", "t": 1}]}):
        with pytest.raises(policy.PolicyError):
            app.handle("POST", f"/v1/jobs/{job}/sheet", {}, bad)
    inbox = app.cfg.root / "inbox"
    inbox.mkdir(exist_ok=True)
    shutil.copy(media / "talk_zh.mp4", inbox / "big.mp4")
    policy.EXTRA_READ_ROOTS.append(inbox)
    try:
        e = ingest.link_inbox(app.cfg, app.store, job, "big.mp4")
        link = app.store.job_dir(job) / "src" / "big.mp4"
        assert link.is_symlink() and e["kind"] == "inbox_link" and e["duration_s"] > 30
        assert policy.inside(app.store.job_dir(job), "src/big.mp4") == (inbox / "big.mp4").resolve()  # 链接指向 inbox：允许只读使用
        (inbox / "notes.md").write_text("# 报告\n正文", encoding="utf-8")
        ingest.link_inbox(app.cfg, app.store, job, "notes.md")
        import threading  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        from autocut.worker.server import make_server  # noqa: PLC0415

        srv = make_server(app.cfg, app)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:  # 链接文件也能 fetch（agent 读 inbox 里的报告）
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/v1/jobs/{job}/file?path=src/notes.md", timeout=20) as r:
                assert "正文" in r.read().decode()
        finally:
            srv.shutdown()
        (app.store.job_dir(job) / "src" / "evil.mp4").symlink_to("/etc/hosts")
        with pytest.raises(policy.PolicyError):
            policy.inside(app.store.job_dir(job), "src/evil.mp4")  # 指向别处的链接照旧拒绝
        with pytest.raises(policy.PolicyError):
            ingest.link_inbox(app.cfg, app.store, job, "../x.mp4")
    finally:
        policy.EXTRA_READ_ROOTS.remove(inbox)


def test_seams_to_restore_maps_flagged_seam_to_the_cut_segment_between() -> None:
    """核验报告的"落在说话中间的接缝"（成片时间）→ 接缝处被剪掉的片段：确定性吸附，不是放宽核验。"""
    from autocut.plan import Output, Plan, Segment, Source
    from autocut.worker.pipe_cut import seams_to_restore

    segs = [Segment("d1", "a", 0, 10, "keep"), Segment("d2", "a", 10, 11, "cut", "silence"), Segment("d3", "a", 11, 20, "keep"), Segment("d4", "a", 20, 22, "cut", "silence"),
            Segment("d5", "a", 22, 30, "keep")]
    plan = Plan("cut", {"a": Source("a", "a.mp4", "video", 30.0)}, [Output("main", segs)])  # 成片：d1 0–10，d3 10–19，d5 19–27
    assert seams_to_restore(plan, [10.0]) == ["d2"] and seams_to_restore(plan, [19.02]) == ["d4"]
    assert seams_to_restore(plan, [10.0, 19.0]) == ["d2", "d4"] and seams_to_restore(plan, [0.0]) == []


def test_verify_skips_seam_check_when_narration_is_mixed_in(app: App, media: Path, tmp_path: Path) -> None:
    """有配音解说时，成片里的"语音"是解说，检测不出原声的接缝：核验如实标明跳过（原声接缝在加解说之前已核验并自愈），不是放宽。"""
    from autocut.worker import pipe_commentary  # noqa: PLC0415
    from autocut.worker.pipeline import start  # noqa: PLC0415

    job = _commentary_job(app, media)
    wait(app, job, start(app, job, "commentary_prepare", {}, lambda c: pipe_commentary.run_prepare(c))["id"])
    script = {"title": "t", "lines": [{"at": 1.0, "text": "开场先看这一手"}, {"at": 8.0, "text": "这里对面开团了"}]}
    r = wait(app, job, start(app, job, "commentary_finish", {"script": script}, lambda c: pipe_commentary.run_finish(c, tts=_FakeTTS(tmp_path)))["id"])
    assert r["status"] == "ok", r.get("result")
    assert "没有检查接缝" in r["result"]["summary"] or "配音解说" in r["result"]["summary"] or "通过" in r["result"]["summary"]
