"""montage（多素材混剪）：两段式——worker 做手和眼（节拍、镜头库、看图说话），DSH agent 做大脑（按主题挑镜头）。

prepare：autocut montage 给出拍点槽位（BPM、小节、每个槽位多长）→ 镜头库 + 无状态的"看图说话"（眼睛）+ 原声片段 → brief，停在 needs_agent。
finish ：agent 交回 picks（每个槽位放哪个镜头）→ 程序校验（槽位/镜头存在、每个镜头最多用一次）→ 替换槽位里的画面（节拍不变）→ 体检 → render → 预览 → 等确认。
没被 agent 覆盖的槽位沿用配方默认选择并写进说明。
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autocut.analysis import Media
from autocut.plan import Plan
from autocut.recipes.montage import describe_video, shot_bounds, windows_for
from autocut.worker import delivery, ingest
from autocut.worker import signals
from autocut.worker.pipeline import Ctx, PipelineError, start

if TYPE_CHECKING:
    from autocut.worker.server import App

log = logging.getLogger(__name__)
VIDEO_EXT = {"mp4", "mov", "mkv", "webm", "m4v"}
AUDIO_EXT = {"mp3", "wav", "m4a", "flac", "aac"}
MAX_SHOTS_PER_SOURCE = 14
BATCH = 10


def shot_catalog(media: Media, label: str, limit: int = MAX_SHOTS_PER_SOURCE) -> list[dict[str, Any]]:
    """素材的镜头表：用场景切点切镜头，镜头太多时均匀抽 limit 个。"""
    shots = shot_bounds(media.duration, media.cuts())
    if len(shots) > limit:
        step = len(shots) / limit
        shots = [shots[int(i * step)] for i in range(limit)]
    return [{"id": f"{label}{i + 1}", "a": round(a, 2), "b": round(b, 2), "len": round(b - a, 2)} for i, (a, b) in enumerate(shots)]


def validate_picks(n_slots: int, ids: set[str]):
    def _v(obj: Any) -> None:
        p = obj["picks"]
        if not isinstance(p, list) or not p:
            raise ValueError("picks 不能为空")
        for x in p:
            if x["shot"] not in ids:
                raise ValueError(f"镜头 {x['shot']} 不在镜头库里")
            if not 1 <= int(x["slot"]) <= n_slots:
                raise ValueError(f"槽位 {x['slot']} 超出 1–{n_slots}")
    return _v


def extract_audio(src: Path, dst: Path) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", str(dst)], check=True, capture_output=True, timeout=300)
    return dst


def run_prepare(ctx: Ctx) -> dict[str, Any]:
    o = ctx.opts
    job = ctx.app.store.get_job(ctx.job_id)
    prov = ingest.read_json(ctx.dir / "provenance.json", {"sources": []})
    files = [s["file"] for s in prov["sources"] if s.get("file")]
    vids = [f for f in (o.get("clips") or files) if f.rsplit(".", 1)[-1].lower() in VIDEO_EXT and f != o.get("music")]
    music = o.get("music") or next((f for f in files if f.rsplit(".", 1)[-1].lower() in AUDIO_EXT), None)
    if not vids:
        raise PipelineError("no_clips", "这个 job 里没有视频素材", "先 ingest / upload 至少 2 段视频")
    if not music:
        raise PipelineError("no_music", "混剪需要 BGM", "ingest/upload 一首音频，或 music=src/xxx.mp4（从视频里取音轨）")
    theme = str(o.get("theme") or (job.get("config") or {}).get("purpose") or "")
    length = float(o.get("length", 20))
    aspect = str(o.get("aspect", "9:16"))
    if music.rsplit(".", 1)[-1].lower() in VIDEO_EXT:
        ctx.stage("music", f"从 {music} 取音轨")
        music = str(extract_audio(ctx.path(music), ctx.dir / "work" / "bgm.wav").relative_to(ctx.dir))
    base_rel = "work/montage.base.autocut.json"
    ctx.stage("beats", f"{len(vids)} 个素材 + BGM，找拍点")
    ctx.autocut("montage", [*vids, "--music", music, "--length", str(length), "--aspect", aspect, "-o", base_rel, "--force", "--cut-every", str(o.get("cut_every", "auto"))])
    plan = Plan.load(ctx.path(base_rel))
    out = plan.output()
    src_ids = [sid for sid in plan.sources if sid != "music"]

    ctx.stage("eyes", "镜头库：每个镜头取中间帧拼图（确定性，不调任何模型）")
    catalog: list[dict[str, Any]] = []
    fdir = ctx.dir / "work" / "montage-frames"
    fdir.mkdir(parents=True, exist_ok=True)
    medias: dict[str, Media] = {sid: Media(plan.source_path(sid)) for sid in src_ids}
    for k, sid in enumerate(src_ids):
        m = medias[sid]
        for c in shot_catalog(m, chr(ord("A") + k)):
            c["src"] = sid
            words = None
            if o.get("use_asr", True):
                try:
                    words = m.words()  # 有缓存则零成本；没有就当没有原声文字
                except Exception as e:  # noqa: BLE001
                    log.warning("ASR 不可用，镜头不带原声文字：%s", e)
            if words:
                c["speech"] = "".join(str(w["text"]) for w in words if c["a"] <= float(w["start"]) <= c["b"])[:40]
            catalog.append(c)
    sheets = []
    for b in range(0, len(catalog), 12):
        rows = catalog[b:b + 12]
        p = signals.contact_sheet([(medias[r["src"]].path, (r["a"] + r["b"]) / 2, r["id"]) for r in rows], fdir, f"shots{b // 12 + 1}")
        sheets.append({"path": str(p.relative_to(ctx.dir)), "shots": [r["id"] for r in rows]})
    (ctx.dir / "work" / "montage-catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    slots = [{"slot": i + 1, "seconds": round(float(s.slot), 2), "bar": s.evidence.get("bar"), "beat": s.evidence.get("beat")} for i, s in enumerate(out.segments)]
    brief = {"theme": theme, "bpm": plan.options.get("bpm"), "bars": plan.options.get("bars"), "duration": round(out.duration, 1), "slots": slots,
             "sheets": sheets, "catalog": [{"id": c["id"], "source": c["src"], "seconds": c["len"], "from": c["a"], **({"speech": c["speech"]} if c.get("speech") else {})} for c in catalog],
             "rules": "sheets 是镜头中间帧拼图，格子上的标签就是镜头 id：用 ar fetch 拉到本机再 read_image 看；每个槽位挑一个镜头；每个镜头最多用一次；时长 ≥ 槽位的优先（短的会定格补齐）；相邻槽位画面要有对比、同一素材不要连着用；第一个槽位用最抓眼的，最后一个有收束感；整体服务主题、有起伏",
             "picks_schema": [{"slot": 1, "shot": "A3", "why": "一句话"}]}
    return {"ok": True, "state": "needs_agent", "needs": "picks", "brief": brief, "plan": base_rel,
            "next": [{"cmd": f"autocut-remote pipeline {ctx.job_id} montage_finish picks='[{{\"slot\":1,\"shot\":\"A3\",\"why\":\"…\"}}]'", "why": "挑好镜头后提交（程序校验、替换槽位、出片）"}]}


def run_finish(ctx: Ctx) -> dict[str, Any]:
    o = ctx.opts
    base_rel = "work/montage.base.autocut.json"
    if not ctx.path(base_rel).exists() or not ctx.path("work/montage-catalog.json").exists():
        raise PipelineError("not_prepared", "还没有 montage_prepare 的结果", f"先 ar pipeline {ctx.job_id} montage_prepare", "validate")
    plan = Plan.load(ctx.path(base_rel))
    out = plan.output()
    catalog = json.loads(ctx.path("work/montage-catalog.json").read_text(encoding="utf-8"))
    picks = o.get("picks")
    if isinstance(picks, str):
        try:
            picks = json.loads(picks)
        except ValueError as e:
            raise PipelineError("bad_picks", f"picks 不是合法 JSON：{e}", '提交 [{"slot": 1, "shot": "A3", "why": "…"}, …]', "validate") from None
    if not isinstance(picks, list) or not picks:
        raise PipelineError("picks_required", "需要 picks（每个槽位挑哪个镜头）", "先 montage_prepare 拿 brief，再挑镜头提交", "validate")
    ctx.stage("validate", "校验挑选")
    try:
        validate_picks(len(out.segments), {c["id"] for c in catalog})({"picks": picks})
    except (ValueError, KeyError, TypeError) as e:
        raise PipelineError("bad_picks", f"挑选不合格：{e}", f"槽位 1–{len(out.segments)}，镜头 id 必须来自 brief.catalog", "validate") from None
    used: set[str] = set()
    by_slot: dict[int, dict[str, Any]] = {}
    dups: list[str] = []
    for p in picks:
        k = int(p["slot"])
        if p["shot"] in used or k in by_slot:
            dups.append(f"槽位{k}/镜头{p['shot']}")
            continue
        by_slot[k] = p
        used.add(p["shot"])
    if dups:
        raise PipelineError("duplicate_picks", f"镜头或槽位重复：{dups}", "每个镜头最多用一次，每个槽位只挑一个", "validate", details=dups)
    cat = {c["id"]: c for c in catalog}
    src_ids = [sid for sid in plan.sources if sid != "music"]
    medias = {sid: Media(plan.source_path(sid)) for sid in src_ids}
    descs: dict[str, Any] = {}  # 每个素材只解码一次（describe_video 要把整段视频解成小图，长素材要几十秒）
    kept_default, replaced = 0, 0
    ctx.stage("apply", "把挑的镜头放进拍点槽位")
    for i, seg in enumerate(out.segments, 1):
        p = by_slot.get(i)
        if not p:
            kept_default += 1
            continue
        c = cat[p["shot"]]
        m = medias[c["src"]]
        if c["src"] not in descs:
            descs[c["src"]] = describe_video(m.path, m.duration, m.cuts())
        wins = windows_for(seg.slot, [[(c["a"], c["b"])] if sid == c["src"] else [] for sid in src_ids], [descs[c["src"]]] * len(src_ids))
        wins = [w for w in wins if src_ids[w.src] == c["src"]]
        if not wins:
            kept_default += 1
            continue
        w = max(wins, key=lambda x: x.motion)
        seg.source, seg.start, seg.end = c["src"], round(w.start, 4), round(w.end, 4)
        seg.detail = f"agent 选镜头 {c['id']}：{str(p.get('why', ''))[:50]}" + ("；镜头不够长，定格补齐" if w.freeze else "") + f" · 第 {seg.evidence.get('bar')} 小节第 {seg.evidence.get('beat')} 拍"
        seg.evidence["pick"] = c["id"]
        replaced += 1
    note = f"混剪镜头由 DSH agent 按主题挑选：{replaced}/{len(out.segments)} 个槽位替换{('，' + str(kept_default) + ' 个槽位沿用配方默认选择') if kept_default else ''}"
    plan.notes.append(note)
    plan_rel = "work/montage.autocut.json"
    plan.save(ctx.path(plan_rel))
    platform = str(o.get("platform", "douyin"))
    ctx.stage("check-plan", "autocut check")
    chk = ctx.autocut("check", [plan_rel, "--platform", platform, "--no-asr"], allow_fail=True)
    ctx.stage("render")
    ren = ctx.autocut("render", [plan_rel, "--out-dir", "out", "--force", "--loudnorm", "-14"])
    r0 = (ren.get("results") or [{}])[0]
    final = ctx.dir / r0["path"] if r0.get("path") and not Path(r0["path"]).is_absolute() else Path(r0.get("path", ""))
    if not final.is_file():
        final = sorted((ctx.dir / "out").glob("*.mp4"), key=lambda p: p.stat().st_mtime)[-1]
    ctx.stage("preview", "确认用预览")
    prev = delivery.make_preview(final, ctx.dir / "out" / f"preview-{final.stem}.mp4")
    v = r0.get("verify") or {}
    risks = [i for i in (chk.get("items") or []) if i.get("severity") in ("risk", "warn")][:3] if chk.get("ok") else []
    credits = ingest.credits_text(ctx.app.store, ctx.job_id)
    summary = "\n".join(x for x in [
        f"混剪 {out.duration:.0f}s，{len(out.segments)} 个镜头来自 {len({s.source for s in out.segments})}/{len(src_ids)} 个素材，卡 {plan.options.get('bpm')} BPM 的拍点",
        note, f"出片核验：{'通过' if v.get('verified') else '未通过 ' + str(r0.get('warnings') or v)}",
        ("体检提示（留存风险信号，不是爆款保证）：" + "；".join(str(i.get("message", ""))[:60] for i in risks)) if risks else "",
        ("需标注转载：" + credits.replace("\n", " / ")) if credits else ""] if x)
    dv = {"id": "main", "final": str(final.relative_to(ctx.dir)), "preview": str(Path(prev["path"]).relative_to(ctx.dir)), "plan": plan_rel,
          "extras": [p for p in (str(final.relative_to(ctx.dir).with_suffix(".srt")),) if (ctx.dir / p).exists()]}
    delivery.mark_awaiting(ctx.app.store, ctx.job_id, [dv], summary)
    return {"ok": True, "state": "awaiting_confirmation", "deliverables": [dv], "summary": summary,
            "next": [{"cmd": f"autocut-remote fetch {ctx.job_id} {dv['preview']}", "why": "下载预览，用 wechat_send_file 发给用户确认"}]}


def pipeline_prepare(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "montage_prepare", opts, run_prepare)


def pipeline_finish(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "montage_finish", opts, run_finish)
