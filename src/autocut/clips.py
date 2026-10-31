"""agent 自己选段：分页读带句段 ID 的文字稿，按句段 ID 指认起止，程序换算成时间并做硬约束校验。

为什么用 ID 不用时间戳：模型写时间戳会混淆 00:01:55 与 01:55:00、或凭空造一个不在句子边缘的时间；
给句段 ID 后边界由程序吸附到句首/句尾之外的停顿里（复用 highlights 的 Cutter），任何一刀都不落在一句话中间。
本模块只做确定性的判断，不调 LLM；选哪几句由调用方（DSH agent / worker 流水线）决定。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autocut.analysis import Media
from autocut.plan import Captions, Output, Plan, Reframe, Segment, new_segment_id
from autocut.recipes._common import RecipeError, aspect_size, finalize, make_source, mmss, plan_path_for, unique_id
from autocut.recipes.highlights import _SENT_END, Cutter, _sentences, protected_spans

log = logging.getLogger(__name__)
ID_RE = re.compile(r"^s(\d{4,})$")
DEFAULT_LENGTH = (30.0, 60.0)
MIN_GAP = 2.0  # 两条候选之间至少隔这么久（否则观众觉得是同一段）
MAX_OVERLAP = 0.3


class ClipError(RecipeError):
    """add_clip 被硬约束拒绝；violations 里每项 {code, severity, message, fix}。"""

    def __init__(self, message: str, violations: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.violations = violations


@dataclass
class Sentence:
    id: str
    start: float
    end: float
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "start": round(self.start, 2), "end": round(self.end, 2), "at": mmss(self.start), "text": self.text}


def build_sentences(words: list[dict[str, Any]]) -> list[Sentence]:
    return [Sentence(f"s{i + 1:04d}", s, e, t) for i, (s, e, t) in enumerate(_sentences(words))]


def page(sents: list[Sentence], *, offset: int = 0, limit: int = 40, around: float | None = None, max_text: int = 160) -> dict[str, Any]:
    """分页读文字稿。around=秒：以该时刻的句子为中心取一页（先读全局再放大的用法）。"""
    total = len(sents)
    if around is not None and sents:
        idx = min(range(total), key=lambda i: abs((sents[i].start + sents[i].end) / 2 - around))
        offset = max(0, idx - limit // 2)
    offset = max(0, min(offset, max(total - 1, 0)))
    chunk = sents[offset:offset + limit]
    items = []
    for s in chunk:
        d = s.as_dict()
        if len(d["text"]) > max_text:
            d["text"] = d["text"][:max_text] + "…"
        items.append(d)
    nxt = offset + len(chunk)
    return {"total": total, "offset": offset, "showing": len(items), "next_offset": nxt if nxt < total else None, "items": items}


@dataclass
class ClipContext:
    """一个素材的选段上下文：句子、受保护语音区间、切点吸附器。"""

    media: Media | None
    sents: list[Sentence]
    cutter: Cutter
    duration: float
    language: str | None = None
    by_id: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_id = {s.id: i for i, s in enumerate(self.sents)}


def context_from(sents: list[Sentence], spans: list[tuple[float, float]], duration: float) -> ClipContext:
    return ClipContext(None, sents, Cutter(spans, duration), duration)


def make_context(media: Media, language: str | None = None) -> ClipContext:
    words = media.words(language=language)
    if not words:
        raise RecipeError(f"{media.path.name} 没有可用的文字稿（没有语音识别模型，或素材里没有语音）；先跑 `autocut doctor` 看识别是否可用")
    sents = build_sentences(words)
    regions = [tuple(r) for r in media.speech().regions]
    spans = protected_spans(regions, [(s.start, s.end, s.text) for s in sents])
    ctx = ClipContext(media, sents, Cutter(spans, media.duration), media.duration, language)
    log.info("选段上下文：%s %d 句 %d 个受保护语音区间 %.0fs", media.path.name, len(sents), len(spans), media.duration)
    return ctx


def resolve_ids(ctx: ClipContext, first: str, last: str) -> tuple[int, int]:
    for sid in (first, last):
        if sid not in ctx.by_id:
            near = ", ".join(s.id for s in ctx.sents[:3])
            raise ClipError(f"没有句段 {sid}", [{"code": "unknown_sentence", "severity": "error", "message": f"句段 {sid} 不存在（共 {len(ctx.sents)} 句，id 形如 {near}）",
                                              "fix": "用 transcript 分页读出带 id 的文字稿，只能引用其中出现过的 id；不要自己编时间戳"}])
    i, j = ctx.by_id[first], ctx.by_id[last]
    if i > j:
        raise ClipError("起止顺序反了", [{"code": "reversed", "severity": "error", "message": f"起句 {first} 在止句 {last} 之后", "fix": "起句 id 必须小于等于止句 id"}])
    return i, j


def fit_bounds(ctx: ClipContext, i: int, j: int) -> tuple[float, str, float, str]:
    """句 i 到句 j → (起点, 起点怎么吸的, 终点, 终点怎么吸的)：起点在句首之前的停顿里，终点在句尾之后约 0.5s。"""
    s0, s1 = ctx.sents[i].start, ctx.sents[j].end
    prev_end = ctx.sents[i - 1].end if i > 0 else 0.0
    next_start = ctx.sents[j + 1].start if j + 1 < len(ctx.sents) else ctx.duration
    start, how_s = ctx.cutter.snap_start(s0 - 0.25, max(prev_end, s0 - 1.5), s0)
    end, how_e = ctx.cutter.snap_end(s1 + 0.5, s1, min(next_start, s1 + 1.5))
    return start, how_s, end, how_e


def check(ctx: ClipContext, i: int, j: int, start: float, end: float, how_s: str, how_e: str, *, length: tuple[float, float] = DEFAULT_LENGTH,
          others: list[tuple[float, float]] | None = None, forbidden: list[tuple[float, float]] | None = None,
          banned_words: list[str] | None = None) -> list[dict[str, str]]:
    """硬约束校验（程序判断，不靠 LLM）。severity=error 的必须修；warn 的只提醒。"""
    v: list[dict[str, str]] = []

    def add(code: str, sev: str, msg: str, fix: str) -> None:
        v.append({"code": code, "severity": sev, "message": msg, "fix": fix})

    d = end - start
    lmin, lmax = length
    if d < lmin:
        add("too_short", "error", f"时长 {d:.0f}s，短于下限 {lmin:.0f}s", f"往后多包含几句（当前止句 {ctx.sents[j].id}）")
    if d > lmax:
        add("too_long", "error", f"时长 {d:.0f}s，超过上限 {lmax:.0f}s", f"少包含几句（当前起句 {ctx.sents[i].id}）")
    if how_s == "hard" or ctx.cutter.inside(start):
        add("cut_mid_speech", "error", f"起点 {mmss(start)} 落在一句话中间（这里一直有人说话，找不到停顿）", "换一个更靠近句子边缘的起句")
    if how_e == "hard" or ctx.cutter.inside(end):
        add("cut_mid_speech", "error", f"终点 {mmss(end)} 落在一句话中间", "换一个止句，让结尾落在句子结束处")
    first_word_gap = ctx.sents[i].start - start
    if first_word_gap > 1.0:
        add("late_opening", "error", f"开头 {first_word_gap:.1f}s 后才开始说话", "起点离第一句太远，换起句或收紧")
    last = ctx.sents[j]
    nxt = ctx.sents[j + 1] if j + 1 < len(ctx.sents) else None
    if last.text.rstrip()[-1:] not in _SENT_END and nxt is not None and nxt.start - last.end < 0.8:
        add("truncated_ending", "error", f"结尾「{last.text[-12:]}」像是半句话（后面 {nxt.start - last.end:.1f}s 内紧接着还有话）", f"把止句换成 {nxt.id} 或更靠后的句子")
    for (a, b) in others or []:
        ov = max(0.0, min(end, b) - max(start, a))
        if ov / max(min(d, b - a), 1e-6) > MAX_OVERLAP:
            add("overlaps_other", "error", f"和已有候选 {mmss(a)}–{mmss(b)} 重叠 {ov:.0f}s", "选一段不重叠的")
        elif start < b + MIN_GAP and a < end + MIN_GAP:
            add("too_close", "warn", f"离已有候选 {mmss(a)}–{mmss(b)} 不到 {MIN_GAP:.0f}s", "观众会觉得是同一段")
    for (a, b) in forbidden or []:
        if start < b and a < end:
            add("in_forbidden_zone", "error", f"落在不可用区间 {mmss(a)}–{mmss(b)}（广告/赞助/被拒过的段）", "换一段")
    text = "".join(s.text for s in ctx.sents[i:j + 1])
    for w in banned_words or []:
        if w and w in text:
            add("sensitive_word", "error", f"文字里命中敏感词「{w}」", "换一段，或让用户决定")
    return v


def add_clip(plan: Plan, ctx: ClipContext, first: str, last: str, *, title: str, reason: str, confidence: float = 0.7,
             length: tuple[float, float] = DEFAULT_LENGTH, forbidden: list[tuple[float, float]] | None = None,
             banned_words: list[str] | None = None, aspect: str | None = "9:16", reframe: str = "center", selected: bool = False,
             hook: str = "") -> tuple[Output, list[dict[str, str]]]:
    """按句段 ID 新建一个候选 Output 加进方案。有 error 级违例时抛 ClipError（方案不变）；返回 (Output, 违例里的 warn)。"""
    i, j = resolve_ids(ctx, first, last)
    start, how_s, end, how_e = fit_bounds(ctx, i, j)
    others = [(o.kept()[0].start, o.kept()[-1].end) for o in plan.outputs if o.segments]
    viol = check(ctx, i, j, start, end, how_s, how_e, length=length, others=others, forbidden=forbidden, banned_words=banned_words)
    errs = [x for x in viol if x["severity"] == "error"]
    if errs:
        raise ClipError("；".join(x["message"] for x in errs), viol)
    src_id = next(iter(plan.sources))
    n = len(plan.outputs) + 1
    size = aspect_size(aspect)
    text = "".join(s.text for s in ctx.sents[i:j + 1])
    seg = Segment(id=new_segment_id("c", 1), source=src_id, start=float(start), end=float(end), reason="highlight", confidence=round(float(confidence), 2),
                  detail=f"agent 选段 {first}–{last}：{reason}；起点{how_s}、终点{how_e}", text=text[:200], evidence={"first": first, "last": last, "hook": hook})
    media_v = ctx.media.info.video if ctx.media else None
    needs_reframe = bool(size and media_v and abs(size[0] / size[1] - media_v.display_aspect) > 0.02)
    out = Output(id=f"clip_{n}", segments=[seg], width=size[0] if size else None, height=size[1] if size else None,
                 captions=Captions(enabled=True, burn=True), reframe=Reframe(mode="center") if needs_reframe else None,
                 selected=selected, title=title[:60], score=round(float(confidence) * 100), reasons=[reason])
    plan.outputs.append(out)
    return out, [x for x in viol if x["severity"] == "warn"]


def new_plan(media: Media, out: Path | None, *, aspect: str | None = "9:16", language: str | None = None) -> Plan:
    plan_path = plan_path_for(media.path, out)
    sid = unique_id(media.path.stem, set())
    src = make_source(sid, media, plan_path)
    wp = media.words_cache_path(language=language)
    if wp is not None:
        src.words = str(wp)
    plan = Plan(recipe="clips", sources={sid: src}, outputs=[], options={"aspect": aspect, "language": language}, notes=["候选由 agent 按句段 ID 选出（autocut clip），边界已吸附到句子边缘外的停顿"], path=plan_path)
    return plan
