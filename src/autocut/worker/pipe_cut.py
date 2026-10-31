"""cut（口播/录课/实况粗剪）：两段式——autocut 配方只给"提案"，剪不剪由 DSH agent 决定，规则只做校验。

prepare：autocut cut 出提案（每个停顿/重说/口头禅一个候选，带理由、把握度和上下文文字）→ brief，停在 needs_agent。
finish ：agent 交回决定（keep=恢复成保留的候选 id，cut=额外要剪掉的片段 id）→ 规则校验（id 存在、不剪锁定片段、
         "停在句子中间"的剪法自动改回保留并如实报告、最多剪掉 70%）→ 体检 → render → 预览 → 等用户确认。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import json

from autocut.worker import delivery, ingest
from autocut.worker.pipeline import Ctx, PipelineError, start
from autocut.worker.store import read_json

if TYPE_CHECKING:
    from autocut.worker.server import App

log = logging.getLogger(__name__)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


UNCERTAIN_BELOW = 0.75  # 配方的"拿不准"阈值（只用于 brief 里标注，不再自动拍板）
MAX_REMOVED = 0.7


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


def guard_mid_sentence(plan_path: Path) -> dict[str, Any]:
    """规则校验层：配方已标出"停在句子中间"的剪法，不管谁决定的都改回保留（宁可多留，不要把话剪断）。就地改方案。"""
    from autocut.plan import Plan  # noqa: PLC0415

    plan = Plan.load(plan_path)
    n, secs = 0, 0.0
    for seg in plan.output().segments:
        if seg.action == "cut" and "句子中间" in seg.detail and not seg.locked:
            seg.action = "keep"
            seg.detail = f"{seg.detail}（规则校验：停在句子中间，改为保留）".strip()
            seg.evidence["guard_restored"] = True
            n += 1
            secs += seg.out_duration
    if n:
        plan.notes.append(f"规则校验：{n} 处停在句子中间的剪法改回保留，多保留 {secs:.1f}s")
        plan.save()
    return {"restored": n, "seconds": round(secs, 1)}


def seams_to_restore(plan: Any, times: list[float]) -> list[str]:
    """确定性吸附：核验报告里落在说话中间的接缝（成片时间）→ 该接缝处被剪掉的片段 id。把它们改回保留，接缝就消失了。"""
    out = plan.output()
    segs = out.segments
    tl = out.timeline()
    kept_pos = {s.id: i for i, s in enumerate(segs)}
    ids: list[str] = []
    for t in times:
        k = min(range(len(tl)), key=lambda j: abs(tl[j][1] - t)) if tl else None
        if k is None or k == 0:
            continue
        a, b = kept_pos[tl[k - 1][0].id], kept_pos[tl[k][0].id]
        ids += [x.id for x in segs[a + 1:b] if x.action == "cut"]  # 两个相邻保留片段之间被剪掉的那几段
    return list(dict.fromkeys(ids))


def heal_seams(ctx: Ctx, plan_rel: str, out_dir: str, loudnorm: str, rounds: int = 3) -> tuple[dict[str, Any], list[str]]:
    """渲染 → 核验 → 把落在说话中间的接缝处的剪法改回保留 → 重渲，最多 rounds 轮。不放宽核验：剩下的接缝如实报告。返回 (最后一次 render 结果, 被恢复的片段 id)。"""
    from autocut.plan import Plan  # noqa: PLC0415

    healed: list[str] = []
    ren: dict[str, Any] = {}
    for r in range(rounds + 1):
        ctx.stage("render" if r == 0 else f"render-heal{r}", "出片并核验接缝" if r == 0 else f"第 {r} 轮：{len(healed)} 处剪法改回保留后重渲")
        ren = ctx.autocut("render", [plan_rel, "--out-dir", out_dir, "--force", "--loudnorm", loudnorm])
        v = ((ren.get("results") or [{}])[0].get("verify") or {})
        times = [float(t) for t in (v.get("mid_speech_at") or [])]
        if not times or r == rounds:
            break
        plan = Plan.load(ctx.path(plan_rel))
        ids = [i for i in seams_to_restore(plan, times) if i not in healed]
        if not ids:
            break
        for seg in plan.output().segments:
            if seg.id in ids:
                seg.action = "keep"
                seg.detail = f"{seg.detail}（接缝落在说话中间，确定性吸附：改为保留）".strip()
                seg.evidence["seam_healed"] = True
        healed += ids
        plan.notes.append(f"核验发现 {len(times)} 个接缝落在说话中间：把接缝处被剪掉的 {len(ids)} 个片段改回保留（第 {r + 1} 轮）")
        plan.save()
    return ren, healed


def run_prepare(ctx: Ctx) -> dict[str, Any]:
    from autocut.plan import Plan  # noqa: PLC0415

    o = ctx.opts
    src = pick_source(ctx)
    stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    plan_rel = f"work/{stem}.autocut.json"
    ctx.stage("cut", f"{src} pace={o.get('pace', 'natural')}（出提案）")
    args = [src, "--pace", str(o.get("pace", "natural")), "-o", plan_rel, "--force"]
    for flag in ("no_retakes", "no_fillers"):
        if o.get(flag):
            args.append("--" + flag.replace("_", "-"))
    if o.get("lang"):
        args += ["--lang", str(o["lang"])]
    ctx.autocut("cut", args)
    plan = Plan.load(ctx.path(plan_rel))
    out = plan.output()
    segs = out.segments
    cands = []
    for i, s in enumerate(segs):
        if s.action != "cut":
            continue
        before = next((x.text for x in reversed(segs[:i]) if x.text), "")[-30:]
        after = next((x.text for x in segs[i + 1:] if x.text), "")[:30]
        cands.append({"id": s.id, "at": round(s.start, 1), "dur": round(s.end - s.start, 2), "reason": s.reason, "conf": s.confidence, "detail": s.detail, "before": before, "after": after})
    src_total = sum(x.src_duration for x in segs)
    removed = sum(x.src_duration for x in segs if x.action == "cut")
    brief = {"source": src, "source_seconds": round(src_total, 1), "proposed_removed_seconds": round(removed, 1), "candidates_total": len(cands), "candidates": cands[:400], "truncated": max(0, len(cands) - 400),
             "uncertain_below": UNCERTAIN_BELOW,
             "how": "提案里每个候选是配方想剪掉的一段（停顿/重说/口头禅）。你来决定：恢复哪些（keep=[id…]：把握度低、停在句子中间、是修辞性重复、剪了会让语气突兀的），"
                    "还想额外剪掉哪些（cut=[片段 id…]，id 用 `ar run JOB show work/x.autocut.json --around mm:ss` 查）。提交：ar pipeline JOB cut_finish keep='[…]' cut='[…]'；"
                    "规则会拦住：不存在的 id、锁定片段、停在句子中间的剪法（自动改回保留）、剪掉超过 70%"}
    return {"ok": True, "state": "needs_agent", "needs": "cut_decisions", "brief": brief, "plan": plan_rel,
            "next": [{"cmd": f"autocut-remote pipeline {ctx.job_id} cut_finish keep='[]' cut='[]'", "why": "做出决定后提交（空列表 = 接受提案，规则仍会校验）"}]}


def run_finish(ctx: Ctx) -> dict[str, Any]:
    from autocut.plan import Plan  # noqa: PLC0415

    o = ctx.opts
    src = pick_source(ctx)
    stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    plan_rel = f"work/{stem}.autocut.json"
    if not ctx.path(plan_rel).exists():
        raise PipelineError("not_prepared", "还没有 cut_prepare 的提案", f"先 ar pipeline {ctx.job_id} cut_prepare", "validate")
    ctx.stage("validate", "校验 agent 的决定")
    plan = Plan.load(ctx.path(plan_rel))
    out = plan.output()
    ids = {s.id: s for s in out.segments}

    def lst(k: str) -> list[str]:
        v = o.get(k) or []
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                raise PipelineError("bad_decisions", f"{k} 不是合法 JSON 数组", f"例如 {k}='[\"d012\",\"d031\"]'", "validate") from None
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise PipelineError("bad_decisions", f"{k} 必须是片段 id 的数组", "", "validate")
        return v

    keep, cut = lst("keep"), lst("cut")
    unknown = [i for i in [*keep, *cut] if i not in ids]
    if unknown:
        raise PipelineError("unknown_segment", f"方案里没有这些片段：{unknown[:10]}", "id 以 brief.candidates 或 `show` 为准，不要编", "validate", details=unknown)
    locked = [i for i in cut if ids[i].locked]
    if locked or set(keep) & set(cut):
        raise PipelineError("bad_decisions", f"锁定的片段不能剪：{locked}；或同一个 id 同时在 keep 和 cut 里：{sorted(set(keep) & set(cut))}", "改正后重新提交", "validate")
    for i in keep:
        ids[i].action = "keep"
        ids[i].detail = f"{ids[i].detail}（agent 决定保留）".strip()
        ids[i].evidence["agent"] = "keep"
    for i in cut:
        ids[i].action = "cut"
        ids[i].detail = f"{ids[i].detail}（agent 决定剪掉）".strip()
        ids[i].evidence["agent"] = "cut"
    plan.notes.append(f"剪辑决定由 DSH agent 做出：恢复 {len(keep)} 个、额外剪掉 {len(cut)} 个提案外片段；规则只做校验")
    plan.save()
    guard = guard_mid_sentence(ctx.path(plan_rel))
    plan = Plan.load(ctx.path(plan_rel))
    summ = plan.summary()["outputs"][0]
    total = sum(x.src_duration for x in plan.output().segments)
    if total and float(summ.get("removed", 0)) / total > MAX_REMOVED:
        raise PipelineError("too_much_removed", f"这样会剪掉 {summ.get('removed', 0):.0f}s / {total:.0f}s（>{MAX_REMOVED:.0%}）", "多保留一些再提交；确实要剪这么多请先问用户", "validate")
    platform = str(o.get("platform", "bilibili"))
    ctx.stage("check-plan", platform)
    chk = ctx.autocut("check", [plan_rel, "--platform", platform, "--no-asr"], allow_fail=True)
    ren, healed = heal_seams(ctx, plan_rel, "out", str(o.get("loudnorm", "-14")))
    plan = Plan.load(ctx.path(plan_rel))
    summ = plan.summary()["outputs"][0]
    finals = [x for x in ctx.dir.joinpath("out").glob("*.mp4") if "preview" not in x.name]
    if not finals:
        raise PipelineError("no_output", "render 成功但没有找到成片", "看 render 的 JSON", "render")
    final = sorted(finals, key=lambda p: p.stat().st_mtime)[-1]
    ctx.stage("preview", "确认用预览")
    prev = delivery.make_preview(final, ctx.dir / "out" / f"preview-{final.stem}.mp4")
    credits = ingest.credits_text(ctx.app.store, ctx.job_id)
    risks = [i for i in (chk.get("items") or []) if i.get("severity") in ("risk", "warn")][:5] if chk.get("ok") else []
    lines = [f"{summ.get('source_span', total):.0f}s → {summ.get('duration', 0):.0f}s，剪掉 {summ.get('removed', 0):.0f}s（{summ.get('cut', {})}）",
             f"剪辑决定由 DSH agent 做出（恢复 {len(keep)} 个候选，额外剪掉 {len(cut)} 个）；规则校验把 {guard['restored']} 处停在句子中间的剪法改回保留（多留 {guard['seconds']}s）；核验后又把 {len(healed)} 处接缝落在说话中间的剪法改回保留",
             f"出片核验：{_verify(ren)}"]
    if risks:
        lines.append("体检提示（留存风险信号，不是爆款保证）：" + "；".join(str(r.get("message", ""))[:80] for r in risks))
    if credits:
        lines.append("需标注转载：" + credits.replace("\n", " / "))
    summary = "\n".join(lines)
    dv = {"id": "main", "final": f"out/{final.name}", "preview": f"out/preview-{final.stem}.mp4", "plan": plan_rel, "extras": [p for p in (f"out/{final.stem}.srt",) if (ctx.dir / p).exists()]}
    delivery.mark_awaiting(ctx.app.store, ctx.job_id, [dv], summary)
    return {"ok": True, "deliverables": [dv], "preview": prev, "summary": summary, "state": "awaiting_confirmation", "guard": guard, "healed": healed,
            "next": [{"cmd": f"autocut-remote fetch {ctx.job_id} {dv['preview']}", "why": "把预览下载到本机再 wechat_send_file 发给用户确认"}]}


def _verify(ren: dict[str, Any]) -> str:
    outs = ren.get("results") or []
    if not outs:
        return "（render 未返回核验字段）"
    r0 = outs[0]
    v = r0.get("verify") or {}
    if v.get("verified"):
        mid = f"{v.get('joints_mid_speech')} 个落在说话中间" if v.get("joints_mid_speech") is not None else f"接缝检查跳过：{v.get('mid_speech_skipped')}"
        return f"通过（时长漂移 {v.get('av_drift')}s，{v.get('joints')} 个接缝，{mid}）"
    return f"未通过 {r0.get('warnings') or v}"


def pipeline_prepare(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "cut_prepare", opts, run_prepare)


def pipeline_finish(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "cut_finish", opts, run_finish)
