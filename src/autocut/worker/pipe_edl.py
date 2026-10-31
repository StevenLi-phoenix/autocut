"""edl（通用剪辑计划执行器）：DSH agent 读素材摘要后写出结构化的剪辑计划（EDL），worker 当不可信输入校验后确定性地执行。

适合"素材太长、不可能整段分析"的场景（例如 50 小时的游戏录像 + 一份任务报告）：agent 凭报告和关键帧决定取哪些时间段、用多快的速度播，
再配解说。校验：素材在 job 里（含 inbox 链接）、时间范围存在、倍速在 0.25–240、段数 ≤300、成片时长 ≤ max_seconds。
高倍速（≥8x）的段用"只解关键帧"的延时摄影方式生成中间片段（解 50 小时的 HEVC 做不起），其余段直接引用素材。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autocut.analysis import Media
from autocut.plan import Output, Plan, Segment
from autocut.recipes._common import make_source
from autocut.worker import delivery, ingest
from autocut.worker.pipe_commentary import narrate_plan
from autocut.worker.pipe_cut import _verify
from autocut.worker.pipeline import Ctx, PipelineCancelled, PipelineError, start
from autocut.worker.tts import TTS

if TYPE_CHECKING:
    from autocut.worker.server import App

log = logging.getLogger(__name__)
MAX_SEGMENTS = 300
SPEED_MIN, SPEED_MAX = 0.25, 240.0
TIMELAPSE_FROM = 8.0
PLAN_SPEED_MAX = 4.0  # plan.Segment 的倍速上限
OUT_FPS = 30


def load_edl(ctx: Ctx) -> dict[str, Any]:
    raw = ctx.opts.get("edl")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise PipelineError("bad_edl", f"edl 不是合法 JSON：{e}", '{"title":…,"segments":[{"source":"src/x.mp4","start":0,"end":60,"speed":1}],"lines":[…]}', "validate") from None
    if raw is None and ctx.opts.get("edl_file"):
        try:
            raw = json.loads(ctx.path(str(ctx.opts["edl_file"])).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise PipelineError("bad_edl", f"读不了 edl_file：{e}", "先把 EDL 传进 job 的 src/ 目录", "validate") from None
    if not isinstance(raw, dict):
        raise PipelineError("edl_required", "需要 edl（JSON 对象）或 edl_file", "见 skill 里的 EDL 格式", "validate")
    return raw


def validate_edl(ctx: Ctx, edl: dict[str, Any]) -> list[dict[str, Any]]:
    segs = edl.get("segments")
    if not isinstance(segs, list) or not segs:
        raise PipelineError("bad_edl", "segments 不能为空", "至少一段 {source,start,end}", "validate")
    if len(segs) > MAX_SEGMENTS:
        raise PipelineError("too_many_segments", f"{len(segs)} 段超过上限 {MAX_SEGMENTS}", "合并相邻的段，或拆成几条成片", "validate")
    durations: dict[str, float] = {}
    bad: list[str] = []
    out: list[dict[str, Any]] = []
    total = 0.0
    for i, s in enumerate(segs):
        try:
            src = str(s["source"])
            a, b = float(s["start"]), float(s["end"])
            sp = float(s.get("speed", 1.0))
        except (KeyError, TypeError, ValueError):
            bad.append(f"#{i + 1} 缺字段或类型不对（需要 source、start、end，speed 可选）")
            continue
        try:
            p = ctx.path(src)
        except Exception:  # noqa: BLE001
            bad.append(f"#{i + 1} 素材路径不合法：{src}")
            continue
        if src not in durations:
            durations[src] = float(ingest.probe(p).get("duration_s") or 0)
        d = durations[src]
        if not (0 <= a < b <= d + 0.5):
            bad.append(f"#{i + 1} 时间范围 {a:.1f}–{b:.1f} 不在素材 {src}（{d:.1f}s）内")
        if not (SPEED_MIN <= sp <= SPEED_MAX):
            bad.append(f"#{i + 1} 倍速 {sp} 不在 {SPEED_MIN}–{SPEED_MAX}")
        elif PLAN_SPEED_MAX < sp < TIMELAPSE_FROM:  # 方案文件只收 ≤4 的普通变速，≥8 走关键帧延时；中间这一档两边都不收，提交时就说清楚
            bad.append(f"#{i + 1} 倍速 {sp:g} 落在空档：普通变速最大 {PLAN_SPEED_MAX:g}，延时摄影从 {TIMELAPSE_FROM:g} 起；改成 ≤{PLAN_SPEED_MAX:g}（缩短素材区间）或 ≥{TIMELAPSE_FROM:g}")
        total += (b - a) / sp if sp else 0
        out.append({"source": src, "start": a, "end": min(b, d), "speed": sp, "note": str(s.get("note", ""))[:80]})
    if bad:
        raise PipelineError("bad_edl", f"EDL 有 {len(bad)} 处不合格", "按 details 逐条改，时间要在素材时长内（引用的时间戳必须真实存在）", "validate", details=bad[:30])
    cap = float(ctx.opts.get("max_seconds", 900))
    if total > cap or total < 5:
        raise PipelineError("bad_length", f"成片会是 {total:.0f}s，应在 5–{cap:.0f}s 内", "调整各段的倍速或删减段数；确实需要更长请先问用户（max_seconds）", "validate")
    return out


def _timelapse(ctx: Ctx, src: Path, a: float, b: float, speed: float, dst: Path) -> None:
    """只解关键帧的延时摄影：每隔 speed/OUT_FPS 秒取一帧，成片约 (b-a)/speed 秒。"""
    # 只解关键帧（GOP 比取样间隔长时，实际取样点就是关键帧）；时间轴按倍速压缩，fps 滤镜把帧补齐到 OUT_FPS，成片时长恰好是 (b-a)/speed
    vf = f"setpts=(PTS-STARTPTS)/{speed:.4f},fps={OUT_FPS},scale=1920:-2,format=yuv420p"
    cmd = ["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-skip_frame", "nokey", "-i", str(src), "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(dst)]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    while p.poll() is None:
        if ctx.cancelled.is_set():
            p.terminate()
            raise PipelineCancelled
        time.sleep(0.5)
    if p.returncode != 0 or not dst.exists():
        raise PipelineError("timelapse_failed", f"延时摄影失败 {src.name} {a:.0f}-{b:.0f}s：{(p.stderr.read() if p.stderr else '')[-200:]}", "该时间段附近的视频可能损坏；换一段", "timelapse")


def run_finish(ctx: Ctx, *, tts: TTS | None = None) -> dict[str, Any]:
    o = ctx.opts
    ctx.stage("validate", "校验 EDL（agent 的输出当不可信输入）")
    edl = load_edl(ctx)
    segs = validate_edl(ctx, edl)
    stem = "edl"
    tdir = ctx.dir / "work" / "edl-clips"
    tdir.mkdir(parents=True, exist_ok=True)
    plan_path = ctx.path(f"work/{stem}.autocut.json")
    sources: dict[str, Any] = {}
    outsegs: list[Segment] = []
    first_media: Media | None = None
    ctx.stage("timelapse", f"{sum(1 for s in segs if s['speed'] >= TIMELAPSE_FROM)} 段延时摄影（只解关键帧）")
    for i, s in enumerate(segs, 1):
        src_path = ctx.path(s["source"])
        sid = next((k for k, v in sources.items() if v[0] == s["source"]), None)
        if s["speed"] >= TIMELAPSE_FROM:
            clip = tdir / f"tl{i:03d}-{hashlib.md5(json.dumps([s['source'], s['start'], s['end'], s['speed']]).encode()).hexdigest()[:8]}.mp4"
            if not clip.exists():
                with ctx.app.runner.heavy_slot():
                    _timelapse(ctx, src_path, s["start"], s["end"], s["speed"], clip)
            m = Media(clip)
            sid = f"tl{i:03d}"
            sources[sid] = (str(clip), m)
            outsegs.append(Segment(id=f"e{i:03d}", source=sid, start=0.0, end=round(m.duration, 3), reason="shot", confidence=0.9,
                                   detail=f"{s['start']:.0f}–{s['end']:.0f}s 的 {s['speed']:g}x 延时摄影；{s['note']}", mute=True))
        else:
            if sid is None:
                sid = f"s{len(sources) + 1}"
                sources[sid] = (s["source"], Media(src_path))
            outsegs.append(Segment(id=f"e{i:03d}", source=sid, start=s["start"], end=s["end"], speed=s["speed"], reason="manual", confidence=0.9,
                                   detail=f"{s['source']} {s['start']:.0f}–{s['end']:.0f}s {s['speed']:g}x；{s['note']}", mute=bool(o.get("mute_source", s["speed"] != 1.0))))
        if first_media is None:
            first_media = sources[sid][1]
    plan = Plan(recipe="edl", sources={}, outputs=[Output(id="main", segments=outsegs, title=str(edl.get("title", ""))[:60])], path=plan_path)
    for sid, (_, m) in sources.items():
        plan.sources[sid] = make_source(sid, m, plan_path)
    v = first_media.info.video if first_media else None
    out = plan.output()
    if v:
        out.width, out.height, out.fps = (int(o.get("width", 1920)), int(o.get("height", 1080)), OUT_FPS) if o.get("width") else (None, None, float(OUT_FPS))
    plan.notes.append(f"剪辑计划由 DSH agent 写出（{len(segs)} 段），worker 校验后执行；高倍速段为只解关键帧的延时摄影")
    plan.save()
    D = round(out.duration, 2)
    plan_rel = f"work/{stem}.autocut.json"
    title = str(edl.get("title", ""))[:60]
    kept_lines: list[Any] = []
    provs: list[str] = []
    cplan_rel = plan_rel
    if edl.get("lines"):
        script = {"title": title, "lines": edl["lines"]}
        cplan_rel, title, _lines, kept_lines, provs = narrate_plan(ctx, plan, D, script, stem, int(o.get("max_lines", 80)), tts or TTS(ctx.app.cfg))
    platform = str(o.get("platform", "bilibili"))
    longest = max((float(ingest.probe(ctx.path(p)).get("duration_s") or 0) for p in {s["source"] for s in segs}), default=0.0)
    chk: dict[str, Any] = {}
    if longest <= 3600:  # 体检要把引用到的素材整段做视觉分析（7 小时的 HEVC 要几十分钟）：素材太长就跳过并如实说明
        ctx.stage("check-plan", platform)
        chk = ctx.autocut("check", [cplan_rel, "--platform", platform, "--no-asr"], allow_fail=True)
    ctx.stage("render")
    ren = ctx.autocut("render", [cplan_rel, "--out-dir", "out", "--force", "--loudnorm", str(o.get("loudnorm", "-14"))])
    finals = [x for x in ctx.dir.joinpath("out").glob("*.mp4") if "preview" not in x.name and x.name.startswith(Path(cplan_rel).name.split(".")[0])]
    final = sorted(finals or list(ctx.dir.joinpath("out").glob("*.mp4")), key=lambda p: p.stat().st_mtime)[-1]
    ctx.stage("preview", "确认用预览")
    prev = delivery.make_preview(final, ctx.dir / "out" / f"preview-{final.stem}.mp4")
    risks = [i for i in (chk.get("items") or []) if i.get("severity") in ("risk", "warn")][:4] if chk.get("ok") else []
    credits = ingest.credits_text(ctx.app.store, ctx.job_id)
    src_total = sum(float(ingest.probe(ctx.path(p)).get("duration_s") or 0) for p in {s["source"] for s in segs})
    lines = [f"{D:.0f}s 成片，由 DSH agent 写的剪辑计划（{len(segs)} 段）执行；素材共 {src_total / 3600:.1f} 小时" + (f"，{len(kept_lines)} 句解说（TTS {sorted(set(provs))}）" if kept_lines else ""),
             f"标题建议：{title}" if title else "", f"出片核验：{_verify(ren)}"]
    if "say" in provs:
        lines.insert(0, "⚠️ 配音降级：肥鱼不可用，用了 macOS 系统语音")
    if not chk:
        lines.append(f"体检已跳过：引用的素材最长 {longest / 3600:.1f} 小时，整段分析太慢（成片本身的核验照常做了）")
    if risks:
        lines.append("体检提示（留存风险信号，不是爆款保证）：" + "；".join(str(r.get("message", ""))[:70] for r in risks))
    if credits:
        lines.append("需标注转载：" + credits.replace("\n", " / "))
    summary = "\n".join(x for x in lines if x)
    dv = {"id": "main", "final": f"out/{final.name}", "preview": f"out/preview-{final.stem}.mp4", "plan": cplan_rel, "extras": [p for p in (f"out/{final.stem}.srt",) if (ctx.dir / p).exists()]}
    delivery.mark_awaiting(ctx.app.store, ctx.job_id, [dv], summary)
    return {"ok": True, "deliverables": [dv], "preview": prev, "summary": summary, "state": "awaiting_confirmation", "title": title,
            "next": [{"cmd": f"autocut-remote fetch {ctx.job_id} {dv['preview']}", "why": "把预览下载到本机再发给用户确认"}]}


def pipeline_finish(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "edl_finish", opts, run_finish)
