"""commentary（游戏实况解说配音）：两段式——worker 做手和眼，DSH agent 做大脑。

prepare：cut（去停顿）→ 抽帧 + 无状态的"看图说话"（眼睛）→ brief（画面描述 + 原声说话段 + 约束），停在 needs_agent。
         解说稿由 DSH agent 来写（判断/创作在大脑里，worker 不调写作模型）。
finish ：agent 交回 script（{title, lines:[{at, text}]}）→ 程序校验 → TTS（成批、缓存）→ 按槽位定速；放不下的句子
         原样退回 agent 缩短（lines_too_long，附每句的字数上限）→ 配音轨 → 方案（解说压低原声 + 字幕）→ 体检 → render → 预览 → 等确认。
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
VISION_MODEL = "claude-haiku-5-5"  # 眼睛：无状态的看图说话（不是创作）
BATCH = 8


def _stem(src: str) -> str:
    return src.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def run_prepare(ctx: Ctx, *, gateway: Gateway | None = None) -> dict[str, Any]:
    o = ctx.opts
    gw = gateway or Gateway(ctx.app.cfg, ctx.dir)
    src = pick_source(ctx)
    stem = _stem(src)
    plan_rel = f"work/{stem}.autocut.json"
    hint = str(o.get("game") or o.get("hint") or "")

    ctx.stage("cut", f"{src}（实况模式去停顿）")
    ctx.autocut("cut", [src, "--pace", str(o.get("pace", "natural")), "-o", plan_rel, "--force"])
    guard = guard_uncertain(ctx.path(plan_rel), float(o.get("uncertain_below", UNCERTAIN_BELOW))) if o.get("conservative", True) else {"restored": 0, "seconds": 0.0}
    plan = Plan.load(ctx.path(plan_rel))
    out = plan.output()
    D = round(out.duration, 2)
    if D < 10:
        raise PipelineError("too_short", f"去掉停顿后只有 {D:.1f}s，不值得配解说", "换一段更长的素材", "cut")
    source_id = out.kept()[0].source

    ctx.stage("eyes", f"{D:.0f}s 成片，抽帧看画面")
    times = C.frame_times(D, int(o.get("max_frames", 40)), float(o.get("frame_every", 0)))
    fdir = ctx.dir / "work" / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for b in range(0, len(times), BATCH):
        chunk = times[b:b + BATCH]
        parts: list[dict[str, Any]] = []
        for k, t in enumerate(chunk):
            sid, st = C.out_to_src(out, t) or (source_id, 0.0)
            parts.append(image_part(C.grab_frame(plan.source_path(sid), st, fdir / f"f{b + k:03d}.jpg")))
        msg = [{"role": "user", "content": [{"type": "text", "text": C.vision_prompt(chunk, hint)}, *parts]}]
        res = gw.chat_json(VISION_MODEL, msg, purpose="eyes", validate=C.validate_frames(len(chunk)), max_tokens=3500, temperature=0.2)
        for t, fr in zip(chunk, res["frames"]):
            frames.append({"t": t, "desc": str(fr["desc"])[:80]})
    words = None
    try:
        from autocut.captions import load_words  # noqa: PLC0415

        words = load_words(plan, source_id)
    except Exception as e:  # noqa: BLE001
        log.warning("取原声文字稿失败（继续，当作原声没人说话）：%s", e)
    speech = [{"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"][:80]} for s in C.speech_intervals(words, out)][:60]
    max_lines = int(o.get("max_lines") or max(4, round(D / 60 * float(o.get("lines_per_min", 5)))))
    brief = {
        "source": src, "game": hint, "duration": D, "frames": frames, "speech": speech,
        "limits": {"max_lines": max_lines, "chars_per_sec": C.CHARS_PER_SEC, "min_gap_s": C.MIN_GAP, "first_line_before_s": 3.0},
        "script_schema": {"title": "一句话标题", "lines": [{"at": "成片时间（秒），贴着对应画面", "text": "台词；字数上限 = (到下一句的间隔 - 0.25) × 4.2 × 1.2"}]},
        "how": "按画面描述和原声写解说稿，用 ar pipeline JOB commentary_finish script_file=work/script.json（先把稿子写成 JSON 文件上传到 job 的 work/ 目录）或 script='{...}' 提交",
    }
    (ctx.dir / "work" / f"{stem}.brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "state": "needs_agent", "needs": "script", "brief": brief, "guard": guard, "plan": plan_rel,
            "next": [{"cmd": f"autocut-remote pipeline {ctx.job_id} commentary_finish script='{{\"title\":\"…\",\"lines\":[{{\"at\":1.5,\"text\":\"…\"}}]}}'", "why": "写好解说稿后提交（程序校验、配音、混音、出片）"}]}


def _load_script(ctx: Ctx) -> dict[str, Any]:
    o = ctx.opts
    raw = o.get("script")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise PipelineError("bad_script", f"script 不是合法 JSON：{e}", "提交 {\"title\": …, \"lines\": [{\"at\": 秒, \"text\": …}]}", "validate") from None
    if raw is None and o.get("script_file"):
        try:
            raw = json.loads(ctx.path(str(o["script_file"])).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise PipelineError("bad_script", f"读不了 script_file：{e}", "先用 PUT .../upload 或 ar upload 把稿子传进 job（路径写 src/ 下的相对路径）", "validate") from None
    if not isinstance(raw, dict):
        raise PipelineError("script_required", "需要 script（JSON 对象）或 script_file", "先 commentary_prepare 拿 brief，再写稿提交", "validate")
    return raw


def run_finish(ctx: Ctx, *, tts: TTS | None = None) -> dict[str, Any]:
    o = ctx.opts
    tts = tts or TTS(ctx.app.cfg)
    src = pick_source(ctx)
    stem = _stem(src)
    plan_rel = f"work/{stem}.autocut.json"
    if not ctx.path(plan_rel).exists():
        raise PipelineError("not_prepared", "还没有 commentary_prepare 的结果", f"先 ar pipeline {ctx.job_id} commentary_prepare", "validate")
    plan = Plan.load(ctx.path(plan_rel))
    out = plan.output()
    D = round(out.duration, 2)
    voice = str(o.get("voice") or "vivian")
    tts_style = str(o.get("tts_style") or "cute")
    lang = str(o.get("lang") or "zh")
    tts_lang = "Chinese" if lang.startswith("zh") else "English"
    platform = str(o.get("platform") or "bilibili")
    brief = json.loads((ctx.dir / "work" / f"{stem}.brief.json").read_text(encoding="utf-8")) if (ctx.dir / "work" / f"{stem}.brief.json").exists() else {}
    max_lines = int((brief.get("limits") or {}).get("max_lines", 12))

    ctx.stage("validate", "校验解说稿")
    script = _load_script(ctx)
    try:
        C.validate_script(D, max_lines)(script)
    except (ValueError, KeyError, TypeError) as e:
        raise PipelineError("bad_script", f"解说稿不合格：{e}", f"成片时长 {D}s，at 要在 0–{D}s 内，最多 {max_lines + 3} 句，每句非空文字", "validate") from None
    title = str(script.get("title", ""))[:60]
    lines = C.clean_lines(script["lines"], D)

    ctx.stage("tts", f"{len(lines)} 句")
    _synth(tts, lines, voice, tts_lang, tts_style)
    bad = C.fit(lines, D)
    if bad:  # 放不下的句子退回大脑去缩短：缩短是创作，不在 worker 里做
        det = [{"k": i, "at": lines[i].at, "text": lines[i].text, "max_chars": C.max_chars_for(C.slot_of(lines, i, D)), "slot_s": round(C.slot_of(lines, i, D), 2)} for i in bad]
        raise PipelineError("lines_too_long", f"{len(bad)} 句放不进它到下一句之间的间隔", "把这些句子改短到 max_chars 以内（或拉开 at），整份稿子重新提交；已合成的句子有缓存，重提交很快", "tts", details=det)
    kept_lines = [ln for ln in lines if not ln.dropped and ln.wav]

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
    provs = sorted({ln.provider for ln in kept_lines})
    plan.notes.append(f"解说配音：{len(kept_lines)} 句（TTS {provs}，原声压低 {o.get('duck_db', 14)}dB），解说稿由 DSH agent 撰写")
    plan.save(cpath)

    ctx.stage("check-plan", platform)
    chk = ctx.autocut("check", [cplan_rel, "--platform", platform, "--no-asr"], allow_fail=True)
    ctx.stage("render")
    ren = ctx.autocut("render", [cplan_rel, "--out-dir", "out", "--force", "--loudnorm", str(o.get("loudnorm", "-14"))])
    finals = list(ctx.dir.joinpath("out").glob(f"{stem}.commentary*.mp4")) or [x for x in ctx.dir.joinpath("out").glob("*.mp4") if "preview" not in x.name]
    final = sorted(finals, key=lambda p: p.stat().st_mtime)[-1]
    ctx.stage("preview", "确认用预览")
    prev = delivery.make_preview(final, ctx.dir / "out" / f"preview-{final.stem}.mp4")

    script_rel = f"out/{stem}.commentary.script.json"
    (ctx.dir / script_rel).write_text(json.dumps({"title": title, "duration": D, "lines": [{"at": ln.at, "text": ln.text, "dur": round(ln.dur, 2), "speed": ln.speed, "provider": ln.provider} for ln in lines]},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    summary_lines = [
        "⚠️ 配音降级：肥鱼 TTS 不可用，用了 macOS 系统语音（机械感明显），建议等肥鱼恢复后重做" if "say" in provs else "",
        f"解说配音版：{D:.0f}s（去停顿后），{len(kept_lines)} 句解说，原声压低 {o.get('duck_db', 14)}dB，字幕{'已烧录' if o.get('burn_captions', True) else '单独文件'}",
        f"标题建议：{title}" if title else "",
        f"出片核验：{_verify(ren)}",
        f"TTS：{tts.stats}",
    ]
    risks = [i for i in (chk.get("items") or []) if i.get("severity") in ("risk", "warn")][:4] if chk.get("ok") else []
    if risks:
        summary_lines.append("体检提示（留存风险信号，不是爆款保证）：" + "；".join(str(r.get("message", ""))[:70] for r in risks))
    credits = ingest.credits_text(ctx.app.store, ctx.job_id)
    if credits:
        summary_lines.append("需标注转载：" + credits.replace("\n", " / "))
    summary = "\n".join(x for x in summary_lines if x)
    dv = {"id": "main", "final": f"out/{final.name}", "preview": f"out/preview-{final.stem}.mp4", "plan": cplan_rel, "script": script_rel,
          "extras": [p for p in (f"out/{final.stem}.srt", script_rel) if (ctx.dir / p).exists()]}
    delivery.mark_awaiting(ctx.app.store, ctx.job_id, [dv], summary)
    return {"ok": True, "deliverables": [dv], "preview": prev, "summary": summary, "state": "awaiting_confirmation", "title": title,
            "next": [{"cmd": f"autocut-remote fetch {ctx.job_id} {dv['preview']}", "why": "把预览下载到本机再 wechat_send_file 发给用户确认"}]}


def _synth(tts: TTS, lines: list[C.Line], voice: str, lang: str, style: str = "cute") -> None:
    todo = [ln for ln in lines if ln.wav is None]
    if not todo:
        return
    try:
        res = tts.synth_many([ln.text for ln in todo], voice=voice, lang=lang, style=style)
    except TTSError as e:
        raise PipelineError("tts_failed", f"配音合成失败：{e}", "肥鱼（laptop:19880）是否在线、WSL 是否在重启；稍后重试", "tts") from e
    for ln, (p, d, prov) in zip(todo, res):
        ln.wav, ln.dur, ln.provider = p, d, prov


def pipeline_prepare(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "commentary_prepare", opts, run_prepare)


def pipeline_finish(app: App, job_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    return start(app, job_id, "commentary_finish", opts, run_finish)
