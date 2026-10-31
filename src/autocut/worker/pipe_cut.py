"""cut 流水线（MVP-A 口播/录课/实况粗剪）：cut → check → render → 预览 → 摘要，停在"等用户确认"。

不选段，没有选段风险；所有风险信号（拿不准处数、体检条目、核验结果）都写进摘要，让 agent 如实转告。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autocut.worker import delivery, ingest
from autocut.worker.pipeline import Ctx, PipelineError, start
from autocut.worker.store import read_json

if TYPE_CHECKING:
    from autocut.worker.server import App

log = logging.getLogger(__name__)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


UNCERTAIN_BELOW = 0.75


def guard_uncertain(plan_path: Path, below: float) -> dict[str, Any]:
    """无人核对时的保守策略：置信度低于阈值的"剪"全部改回"留"（宁可多留，不要把话剪断）。就地改方案，revision +1。"""
    from autocut.plan import Plan  # noqa: PLC0415

    plan = Plan.load(plan_path)
    n, secs = 0, 0.0
    for seg in plan.output().segments:
        if seg.action == "cut" and seg.confidence < below and not seg.locked:
            seg.action = "keep"
            seg.detail = f"{seg.detail}（流水线保守策略：把握度 {seg.confidence:.2f} < {below}，改为保留）".strip()
            seg.evidence["guard_restored"] = True
            n += 1
            secs += seg.out_duration
    if n:
        plan.notes.append(f"流水线保守策略：{n} 处把握度低于 {below} 的剪法改回保留，多保留 {secs:.1f}s（无人逐条核对时宁可多留）")
        plan.save()
    log.info("guard：%s 处改回保留 %.1fs（阈值 %.2f）", n, secs, below)
    return {"restored": n, "seconds": round(secs, 1), "below": below}


def pick_source(ctx: Ctx) -> str:
    """opts.source 优先；否则取 provenance 里第一个视频素材。"""
    if ctx.opts.get("source"):
        ctx.path(str(ctx.opts["source"]))
        return str(ctx.opts["source"])
    prov = read_json(ctx.dir / "provenance.json", {"sources": []})
    for s in prov["sources"]:
        if s.get("file") and s["file"].rsplit(".", 1)[-1].lower() in {e[1:] for e in VIDEO_EXT}:
            return s["file"]
    raise PipelineError("no_source", "这个 job 里还没有视频素材", "先 ingest（--url）或 upload，再跑流水线")


def run_cut(ctx: Ctx) -> dict[str, Any]:
    o = ctx.opts
    src = pick_source(ctx)
    stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    plan = f"work/{stem}.autocut.json"
    pace = str(o.get("pace", "natural"))
    platform = str(o.get("platform", "bilibili"))

    ctx.stage("cut", f"{src} pace={pace}")
    args = [src, "--pace", pace, "-o", plan, "--force"]
    for flag in ("no_retakes", "no_fillers"):
        if o.get(flag):
            args.append("--" + flag.replace("_", "-"))
    if o.get("lang"):
        args += ["--lang", str(o["lang"])]
    cut = ctx.autocut("cut", args)
    summ = cut.get("summary", {})

    guard = {"restored": 0, "seconds": 0.0}
    if o.get("conservative", True):
        ctx.stage("guard", "拿不准的剪法一律改成保留")
        guard = guard_uncertain(ctx.path(plan), float(o.get("uncertain_below", UNCERTAIN_BELOW)))

    ctx.stage("check-plan", platform)
    chk = ctx.autocut("check", [plan, "--platform", platform, "--no-asr"], allow_fail=True)

    ctx.stage("render")
    out_dir = "out"
    ren = ctx.autocut("render", [plan, "--out-dir", out_dir, "--force", "--loudnorm", str(o.get("loudnorm", "-16"))])
    finals = [x for x in ctx.dir.joinpath(out_dir).glob("*.mp4") if "preview" not in x.name]
    if not finals:
        raise PipelineError("no_output", "render 成功但没有找到成片", "看 render 的 JSON", "render")
    final = sorted(finals, key=lambda p: p.stat().st_mtime)[-1]
    rel_final = f"{out_dir}/{final.name}"

    ctx.stage("preview", "≤23MiB 确认用")
    prev = delivery.make_preview(final, ctx.dir / out_dir / f"preview-{final.stem}.mp4", ffmpeg="ffmpeg")

    verify = _verify(ren)
    credits = ingest.credits_text(ctx.app.store, ctx.job_id)
    risks = [i for i in (chk.get("items") or []) if i.get("severity") in ("risk", "warn")][:5] if chk.get("ok") else []
    summary_lines = [
        f"{summ.get('source_span', 0):.0f}s → {summ.get('duration', 0):.0f}s，剪掉 {summ.get('removed', 0):.0f}s（{summ.get('cut', {})}）",
        f"拿不准 {summ.get('uncertain_total', 0)} 处，其中 {guard['restored']} 处剪法被保守策略改回保留（多保留 {guard['seconds']:.0f}s），没有逐条人工核对",
        f"出片核验：{verify}",
    ]
    if chk.get("ok") and risks:
        summary_lines.append("体检提示（留存风险信号，不是爆款保证）：" + "；".join(str(r.get("message", ""))[:80] for r in risks))
    if credits:
        summary_lines.append("需标注转载：" + credits.replace("\n", " / "))
    summary = "\n".join(summary_lines)
    dv = {"id": "main", "final": rel_final, "preview": f"{out_dir}/{final.name.replace(final.stem, 'preview-' + final.stem)}", "plan": plan,
          "extras": [p for p in (f"{out_dir}/{final.stem}.srt",) if (ctx.dir / p).exists()]}
    delivery.mark_awaiting(ctx.app.store, ctx.job_id, [dv], summary)
    return {"ok": True, "deliverables": [dv], "preview": prev, "summary": summary, "state": "awaiting_confirmation",
            "guard": guard, "cut": {"plan": plan, "summary": {k: summ.get(k) for k in ("source_span", "duration", "removed", "cut", "uncertain_total", "degraded")}},
            "next": [{"cmd": f"autocut-remote fetch {ctx.job_id} {dv['preview']}", "why": "把预览下载到本机再 wechat_send_file 发给用户确认"}]}


def _verify(ren: dict[str, Any]) -> str:
    outs = ren.get("results") or []
    if not outs:
        return "（render 未返回核验字段）"
    r0 = outs[0]
    v = r0.get("verify") or {}
    if v.get("verified"):
        return f"通过（时长漂移 {v.get('av_drift')}s，{v.get('joints')} 个接缝，{v.get('joints_mid_speech')} 个落在说话中间）"
    return f"未通过 {r0.get('warnings') or v}"


def pipeline_cut(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "cut", opts, run_cut)
