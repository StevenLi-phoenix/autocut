"""clips（长视频 → N 条竖屏短片）：DSH agent 选段（大脑），worker 做硬约束、渲染核验、确定性 QA、预览（手和眼）。autocut 里没有任何 LLM 调用。

agent 的工作（用 autocut CLI，经 `ar run`）：`transcript` 读带句段 ID 的文字稿 → `clip` 按 ID 指认候选（程序吸附边界并做硬约束：
时长/不切断话/开头有话/不是半句/不悬空开头/不重叠/不在禁区）→ 反复提名直到有足够候选；选段的好坏由 agent 自己判断。
clips_deliver：只渲染 agent 选中的候选（外加备选；敏感词命中直接拒绝）→ render 核验 → 确定性 QA（黑屏/冻结，ffmpeg blackdetect/freezedetect）→ 预览 → 等用户确认。
被拒后 agent 带着原因码和禁区（ar status 里的 rejections）重新提名，只补被拒的条数；最多 3 轮；通过率低于 50% 自动多备选。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autocut.analysis import Media
from autocut.plan import Plan
from autocut.recipes._common import mmss
from autocut.worker import delivery, ingest
from autocut.worker.pipeline import Ctx, PipelineError, start

if TYPE_CHECKING:
    from autocut.worker.server import App

log = logging.getLogger(__name__)
MAX_ROUNDS = 3


def _plan(ctx: Ctx) -> tuple[str, Plan]:
    rel = ctx.opts.get("plan")
    if not rel:
        raise PipelineError("plan_required", "需要 plan（clips 方案路径）", "先用 `ar run JOB clip src/x.mp4 --from sID --to sID …` 建候选，方案默认在 src/ 或 -o 指定的 work/ 下", "")
    p = ctx.path(str(rel))
    if not p.exists():
        raise PipelineError("plan_not_found", f"找不到方案 {rel}", "路径以 `clip` 返回的 plan 字段为准", "")
    plan = Plan.load(p)
    if plan.recipe != "clips":
        raise PipelineError("not_clips_plan", f"{rel} 是 {plan.recipe} 方案", "clips 流水线只处理 `autocut clip` 建的方案", "")
    return str(rel), plan


def _clip_text(plan: Plan, out: Any) -> str:
    from autocut.captions import load_words  # noqa: PLC0415

    seg = out.segments[0]
    words = load_words(plan, seg.source) or []
    toks = [str(w["text"]) for w in words if seg.start - 0.2 <= float(w["start"]) and float(w["end"]) <= seg.end + 0.2]
    return " ".join(toks) if toks and toks[0].isascii() else "".join(toks)


def _clip_text(plan: Plan, out: Any) -> str:
    from autocut.captions import load_words  # noqa: PLC0415

    seg = out.segments[0]
    words = load_words(plan, seg.source) or []
    toks = [str(w["text"]) for w in words if seg.start - 0.2 <= float(w["start"]) and float(w["end"]) <= seg.end + 0.2]
    return " ".join(toks) if toks and toks[0].isascii() else "".join(toks)



# ---------------------------------------------------------------- 交付


def run_deliver(ctx: Ctx) -> dict[str, Any]:
    o = ctx.opts
    store = ctx.app.store
    rel, plan = _plan(ctx)
    select = list(o.get("select") or [])
    backups = [b for b in (o.get("backups") or []) if b not in select]
    if not select:
        raise PipelineError("select_required", "需要 select（要交付的候选 id 列表）", "先用 `clip` 建候选，再把要交付的 id 放进 select", "")
    unknown = [i for i in [*select, *backups] if i not in {x.id for x in plan.outputs}]
    if unknown:
        raise PipelineError("unknown_output", f"方案里没有这些候选：{unknown}", f"可选：{[x.id for x in plan.outputs]}", "")
    banned = _banned(ctx.app.cfg, o)
    hits = {i: [w for w in banned if w and w in _clip_text(plan, next(x for x in plan.outputs if x.id == i))] for i in [*select, *backups]}
    hits = {i: h for i, h in hits.items() if h}
    if hits:
        raise PipelineError("sensitive_word", f"这些候选命中敏感词：{hits}", "换候选；不要绕过", "", details=hits)
    st0 = delivery.state(store, ctx.job_id)
    carried = delivery.live(st0) if st0.get("status") == "rejected" else []
    rc = delivery.rejection_context(store, ctx.job_id)
    if rc["rounds"] >= MAX_ROUNDS and not o.get("force_round"):
        raise PipelineError("too_many_rounds", f"已经被拒 {rc['rounds']} 轮（上限 {MAX_ROUNDS}）", "请用户给出更明确的要求（想要什么样的片段、不想要什么），再用 force_round=true 重跑", "")
    rnd = rc["rounds"] + 1
    degraded = delivery.pass_rate(ctx.app.cfg.root)["degraded_mode"]
    by_id = {x.id: x for x in plan.outputs}
    for out in plan.outputs:
        out.selected = out.id in select or out.id in backups
    plan.save()

    ctx.stage("signals", "autocut check 信号（不否决，写进说明）")
    for oid in select:
        chk = ctx.autocut("check", [rel, "--platform", str(o.get("platform", "douyin")), "--output", oid, "--no-asr"], allow_fail=True)
        items = [i for i in (chk.get("items") or []) if i.get("severity") in ("risk", "warn")] if chk.get("ok") else []
        by_id[oid].reasons += [f"体检：{i.get('message', '')[:70]}" for i in items[:2]]
    plan.save()

    to_render = [*select, *backups]
    ctx.stage("render", f"{len(to_render)} 条（含 {len(backups)} 条备选）")
    od = f"out/r{rnd}"
    args = [rel, "--out-dir", od, "--force", "--loudnorm", "-14"]
    for oid in to_render:
        args += ["--output", oid]
    ren = ctx.autocut("render", args)
    results = {r["output"]: r for r in (ren.get("results") or [])}
    ctx.stage("qa", "核验 + 确定性 QA（黑屏/冻结）")
    ok_ids: list[str] = []
    qa_notes: dict[str, str] = {}
    for oid in to_render:
        r = results.get(oid)
        if not r or not (r.get("verify") or {}).get("verified"):
            qa_notes[oid] = f"render 核验未通过：{(r or {}).get('warnings') or (r or {}).get('verify')}"
            continue
        fp = Path(r["path"]) if Path(r["path"]).is_absolute() else ctx.dir / od / Path(r["path"]).name
        qa = _qa(fp)
        if qa["black_s"] > 1.0 or qa["freeze_s"] > 3.0:
            qa_notes[oid] = f"QA 未通过：{qa}"
            continue
        ok_ids.append(oid)
    final_ids = [i for i in select if i in ok_ids]
    spare = [i for i in backups if i in ok_ids]
    for _ in range(len(select) - len(final_ids)):  # 主选没过 QA：自动用备选顶上
        if spare:
            final_ids.append(spare.pop(0))
    if not final_ids:
        raise PipelineError("all_failed_qa", f"渲染/QA 全部失败（{qa_notes}）", "看 out 目录里的成片，或换候选", "qa")
    plan.save()

    ctx.stage("preview", "确认用预览")
    deliverables: list[dict[str, Any]] = []
    for oid in [*final_ids, *spare]:
        out = by_id[oid]
        r = results[oid]
        final = Path(r["path"]) if Path(r["path"]).is_absolute() else ctx.dir / od / Path(r["path"]).name
        prev = delivery.make_preview(final, ctx.dir / od / f"preview-{final.stem}.mp4")
        seg = out.segments[0]
        rel_final = str(final.relative_to(ctx.dir))
        deliverables.append({"id": f"r{rnd}_{oid}", "output": oid, "final": rel_final, "preview": str(Path(prev["path"]).relative_to(ctx.dir)), "plan": rel, "title": out.title,
                             "range": [round(seg.start, 2), round(seg.end, 2)], "at": mmss(seg.start), "dur": round(out.duration, 1), "role": "main" if oid in final_ids else "backup",
                             "reason": out.reasons[0] if out.reasons else "",
                             "extras": [p for p in (str(final.relative_to(ctx.dir).with_suffix(".srt")),) if (ctx.dir / p).exists()]})
    need = max(1, int(o.get("n", len(select))) - len(carried))
    credits = ingest.credits_text(store, ctx.job_id)
    src_dur = Media(plan.source_path(next(iter(plan.sources)))).duration
    lines = [f"从 {mmss(src_dur)} 的长视频里交付 {len(final_ids)} 条竖屏短片（要 {need} 条）" + (f"，另备 {len(spare)} 条备选，可一键替换" if spare else ""),
             *[f"- {d['title']}（原片 {d['at']} 起，{d['dur']}s）：{d['reason'][:60]}" for d in deliverables if d["role"] == "main"],
             "选段由 DSH agent 做出，程序做硬约束校验、渲染核验和黑屏/冻结检查；竖屏为居中裁切" + ("；通过率偏低，已切换多备选模式" if degraded else "")]
    if len(final_ids) < need:
        lines.append(f"只交付了 {len(final_ids)} 条（要 {need} 条）：没有降低标准凑数")
    if qa_notes:
        lines.append("自动 QA 淘汰：" + "；".join(f"{k}: {v[:60]}" for k, v in qa_notes.items()))
    if credits:
        lines.append("需标注转载：" + credits.replace("\n", " / "))
    summary = "\n".join(x for x in lines if x)
    delivery.mark_awaiting(store, ctx.job_id, deliverables, summary, carry=bool(carried))
    return {"ok": True, "state": "awaiting_confirmation", "deliverables": deliverables, "summary": summary, "round": rnd,
            "next": [{"cmd": f"autocut-remote fetch {ctx.job_id} {deliverables[0]['preview']}", "why": "逐条下载预览，用 wechat_send_file 发给用户确认"}]}


def _banned(cfg: Any, o: dict[str, Any]) -> list[str]:
    words = list(o.get("banned_words") or [])
    p = getattr(cfg, "sensitive_words_file", None)
    if p:
        try:
            words += [ln.strip() for ln in Path(p).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
        except OSError as e:
            log.warning("读敏感词表失败 %s：%s", p, e)
    return words


def _qa(final: Path) -> dict[str, float]:
    """确定性 QA：ffmpeg blackdetect + freezedetect 汇总黑屏/冻结总时长。"""
    import re  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(final), "-vf", "blackdetect=d=0.5:pix_th=0.1,freezedetect=n=-60dB:d=2", "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    black = sum(float(x) for x in re.findall(r"black_duration:([0-9.]+)", r.stderr))
    freeze = sum(float(x) for x in re.findall(r"lavfi.freezedetect.freeze_duration: ([0-9.]+)", r.stderr))
    return {"black_s": round(black, 2), "freeze_s": round(freeze, 2)}


def pipeline_deliver(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "clips_deliver", opts, run_deliver)
