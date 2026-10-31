"""commentary 流水线（游戏实况解说配音）。

cut（去停顿）→ 抽帧 + 视觉 LLM → 解说稿 LLM → TTS（成批 + 缓存）→ 定速/缩短 → 配音轨 → 方案（解说压低原声 + 字幕）→ 体检 → render → 预览 → 等确认。
LLM 只通过网关、带预算；原始响应先落盘；画面/字幕里的文字一律当数据。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autocut.analysis import Media
from autocut.plan import Bed, Captions, Plan
from autocut.recipes._common import make_source, stored_path
from autocut.worker import commentary as C
from autocut.worker import delivery, ingest
from autocut.worker.llm import Gateway, image_part
from autocut.worker.pipe_cut import UNCERTAIN_BELOW, _verify, guard_uncertain, pick_source
from autocut.worker.pipeline import Ctx, PipelineError, start
from autocut.worker.tts import TTS, TTSError

if TYPE_CHECKING:
    from autocut.worker.server import App

log = logging.getLogger(__name__)
VISION_MODEL = "claude-haiku-5-5"
WRITER_MODEL = "claude-sonnet-5-5"
BATCH = 8


def run_commentary(ctx: Ctx, *, gateway: Gateway | None = None, tts: TTS | None = None) -> dict[str, Any]:
    o = ctx.opts
    job = ctx.app.store.get_job(ctx.job_id)
    budget = float(((job.get("config") or {}).get("budget_cny")) or 2.0)
    gw = gateway or Gateway(ctx.app.cfg, ctx.dir, budget)
    tts = tts or TTS(ctx.app.cfg)
    src = pick_source(ctx)
    stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    plan_rel = f"work/{stem}.autocut.json"
    hint = str(o.get("game") or o.get("hint") or "")
    style = str(o.get("style") or "轻松幽默、有节奏感、像朋友在旁边看你玩")
    voice = str(o.get("voice") or "vivian")
    lang = str(o.get("lang") or "zh")
    lang_name, tts_lang = ("中文", "Chinese") if lang.startswith("zh") else ("English", "English")
    platform = str(o.get("platform") or "bilibili")

    # 1. 去停顿
    ctx.stage("cut", f"{src}（实况模式去停顿）")
    cut = ctx.autocut("cut", [src, "--pace", str(o.get("pace", "natural")), "-o", plan_rel, "--force"])
    guard = guard_uncertain(ctx.path(plan_rel), float(o.get("uncertain_below", UNCERTAIN_BELOW))) if o.get("conservative", True) else {"restored": 0, "seconds": 0.0}
    plan = Plan.load(ctx.path(plan_rel))
    out = plan.output()
    D = round(out.duration, 2)
    if D < 10:
        raise PipelineError("too_short", f"去掉停顿后只有 {D:.1f}s，不值得配解说", "换一段更长的素材", "cut")
    source_id = out.kept()[0].source

    # 2. 抽帧 + 视觉
    ctx.stage("vision", f"{D:.0f}s 成片，抽帧看画面")
    times = C.frame_times(D, int(o.get("max_frames", 40)), float(o.get("frame_every", 0)))
    fdir = ctx.dir / "work" / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    src_path = plan.source_path(source_id)
    for b in range(0, len(times), BATCH):
        chunk = times[b:b + BATCH]
        parts: list[dict[str, Any]] = []
        for k, t in enumerate(chunk):
            sid, st = C.out_to_src(out, t) or (source_id, 0.0)
            fp = C.grab_frame(plan.source_path(sid), st, fdir / f"f{b + k:03d}.jpg")
            parts.append(image_part(fp))
        msg = [{"role": "user", "content": [{"type": "text", "text": C.vision_prompt(chunk, hint)}, *parts]}]
        res = gw.chat_json(VISION_MODEL, msg, purpose="vision", validate=C.validate_frames(len(chunk)), max_tokens=1500, temperature=0.2)
        for t, fr in zip(chunk, res["frames"]):
            frames.append({"t": t, "desc": str(fr["desc"])[:80]})
    (ctx.dir / "work" / "frames.json").write_text(json.dumps(frames, ensure_ascii=False, indent=1), encoding="utf-8")

    # 3. 解说稿
    ctx.stage("script", f"{len(frames)} 个画面 → 解说稿")
    words = None
    try:
        from autocut.captions import load_words

        words = load_words(plan, source_id)
    except Exception as e:  # noqa: BLE001
        log.warning("取原声文字稿失败（继续，当作原声没人说话）：%s", e)
    speech = C.speech_intervals(words, out)
    max_lines = int(o.get("max_lines") or max(4, round(D / 60 * float(o.get("lines_per_min", 7)))))
    script = gw.chat_json(WRITER_MODEL, [{"role": "user", "content": C.script_prompt(D, frames, speech, hint=hint, style=style, max_lines=max_lines, lang_name=lang_name)}],
                          purpose="script", validate=C.validate_script(D, max_lines), max_tokens=3000, temperature=0.8)
    lines = C.clean_lines(script["lines"], D)
    title = str(script.get("title", ""))[:60]

    # 4. TTS（成批、缓存）+ 定速 / 缩短
    ctx.stage("tts", f"{len(lines)} 句")
    _synth(tts, lines, voice, tts_lang)
    for rnd in range(2):
        bad = C.fit(lines, D)
        if not bad:
            break
        if rnd == 0:
            items = [{"k": i, "text": lines[i].text, "max_chars": C.max_chars_for(C.slot_of(lines, i, D))} for i in bad]
            try:
                short = gw.chat_json(WRITER_MODEL, [{"role": "user", "content": C.shorten_prompt(items)}], purpose="shorten", max_tokens=1500, temperature=0.4,
                                     validate=lambda ob: ob["lines"])
                for r in short["lines"]:
                    k = int(r["k"])
                    if k in bad and str(r["text"]).strip():
                        lines[k].text = str(r["text"]).strip()
                        lines[k].wav = None
                _synth(tts, [ln for ln in lines if ln.wav is None], voice, tts_lang)
            except Exception as e:  # noqa: BLE001 —— 缩短失败就直接丢这些句子，不让整条流水线失败
                log.warning("缩短台词失败：%s", e)
                for i in bad:
                    lines[i].dropped = f"放不下（槽位 {C.slot_of(lines, i, D):.1f}s）"
        else:
            for i in bad:
                lines[i].dropped = f"放不下（槽位 {C.slot_of(lines, i, D):.1f}s，已尝试缩短）"
    kept_lines = [ln for ln in lines if not ln.dropped and ln.wav]
    if len(kept_lines) < 2:
        raise PipelineError("script_unusable", "解说稿放进时间轴后不足 2 句", "换一段更有内容的素材，或调大 max_lines", "tts")

    # 5. 配音轨 + 方案
    ctx.stage("mix", "拼配音轨，写方案")
    narr = ctx.dir / "work" / f"{stem}.narration.wav"
    sub_words = C.build_track(lines, D, narr)
    words_p = ctx.dir / "work" / f"{stem}.narration.words.json"
    words_p.write_text(json.dumps(sub_words, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    cplan_rel = f"work/{stem}.commentary.autocut.json"
    cpath = ctx.path(cplan_rel)
    plan.sources["narr"] = make_source("narr", Media(narr), cpath, kind="audio")
    plan.sources["narr"].words = stored_path(words_p, cpath)
    out.beds = [Bed(source="narr", at=0.0, role="narration", duck_main_db=float(o.get("duck_db", 14.0)))]
    out.captions = Captions(enabled=True, burn=bool(o.get("burn_captions", True)), source="narr", max_chars=int(o.get("caption_chars", 16)))
    plan.recipe = "cut+commentary"
    plan.notes.append(f"解说配音：{len(kept_lines)}/{len(lines)} 句（TTS {sorted({ln.provider for ln in kept_lines})}，原声压低 {o.get('duck_db', 14)}dB）")
    plan.save(cpath)

    ctx.stage("check-plan", platform)
    chk = ctx.autocut("check", [cplan_rel, "--platform", platform, "--no-asr"], allow_fail=True)
    ctx.stage("render")
    ren = ctx.autocut("render", [cplan_rel, "--out-dir", "out", "--force", "--loudnorm", str(o.get("loudnorm", "-16"))])
    finals = [x for x in ctx.dir.joinpath("out").glob(f"{stem}.commentary*.mp4")]
    if not finals:
        finals = [x for x in ctx.dir.joinpath("out").glob("*.mp4") if "preview" not in x.name]
    final = sorted(finals, key=lambda p: p.stat().st_mtime)[-1]
    ctx.stage("preview", "≤23MiB 确认用")
    prev = delivery.make_preview(final, ctx.dir / "out" / f"preview-{final.stem}.mp4")

    script_rel = f"out/{stem}.commentary.script.json"
    (ctx.dir / script_rel).write_text(json.dumps({"title": title, "duration": D, "game": hint, "style": style, "voice": voice,
                                                  "lines": [{"at": ln.at, "text": ln.text, "dur": round(ln.dur, 2), "speed": ln.speed, "provider": ln.provider, "dropped": ln.dropped} for ln in lines]},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    spent = gw.spent_cny()
    dropped = [ln for ln in lines if ln.dropped]
    provs = sorted({ln.provider for ln in kept_lines})
    degraded_tts = [x for x in provs if x == "say"]
    summary_lines = [
        "⚠️ 配音降级：TTS 服务不可用，用了 macOS 系统语音（机械感明显），建议等 TTS 恢复后重做" if degraded_tts else "",
        f"解说配音版：{D:.0f}s（去停顿后），{len(kept_lines)} 句解说" + (f"，{len(dropped)} 句放不下被丢弃" if dropped else "") + f"，原声压低 {o.get('duck_db', 14)}dB，字幕{'已烧录' if o.get('burn_captions', True) else '单独文件'}",
        f"标题建议：{title}" if title else "",
        f"出片核验：{_verify(ren)}",
        f"LLM 花费 ¥{spent:.3f}（上限 ¥{budget:g}），TTS：{tts.stats}",
    ]
    risks = [i for i in (chk.get("items") or []) if i.get("severity") in ("risk", "warn")][:4] if chk.get("ok") else []
    if risks:
        summary_lines.append("体检提示（留存风险信号，不是爆款保证）：" + "；".join(str(r.get("message", ""))[:70] for r in risks))
    credits = ingest.credits_text(ctx.app.store, ctx.job_id)
    if credits:
        summary_lines.append("需标注转载：" + credits.replace("\n", " / "))
    summary = "\n".join(x for x in summary_lines if x)
    dv = {"id": "main", "final": f"out/{final.name}", "preview": f"out/{('preview-' + final.stem)}.mp4", "plan": cplan_rel, "script": script_rel,
          "extras": [p for p in (f"out/{final.stem}.srt", script_rel) if (ctx.dir / p).exists()]}
    delivery.mark_awaiting(ctx.app.store, ctx.job_id, [dv], summary)
    return {"ok": True, "deliverables": [dv], "preview": prev, "summary": summary, "state": "awaiting_confirmation", "title": title,
            "cost_cny": round(spent, 3), "guard": guard,
            "next": [{"cmd": f"autocut-remote fetch {ctx.job_id} {dv['preview']}", "why": "把预览下载到本机再 wechat_send_file 发给用户确认"}]}


def _synth(tts: TTS, lines: list[C.Line], voice: str, lang: str) -> None:
    todo = [ln for ln in lines if ln.wav is None]
    if not todo:
        return
    try:
        res = tts.synth_many([ln.text for ln in todo], voice=voice, lang=lang)
    except TTSError as e:
        raise PipelineError("tts_failed", f"配音合成失败：{e}", "mini 的 TTS 服务（:8098）是否在线；稍后重试", "tts") from e
    for ln, (p, d, prov) in zip(todo, res):
        ln.wav, ln.dur, ln.provider = p, d, prov


def pipeline_commentary(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "commentary", opts, run_commentary)
