"""字幕：从逐词文字稿生成跟着剪辑走的字幕（C3：剪完字幕必须同步，不能重做一遍）。

流程：素材时间的词 → 只留落在保留片段里的 → 映射到输出时间（slot、speed 都算）→ 按标点和 max_chars 断行 →
整理成 Cue（至少显示 MIN_CUE 秒，互不重叠）。SRT 是侧车文件，烧录用的 ASS 由 render 生成。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autocut.plan import Output, Plan

log = logging.getLogger(__name__)

MIN_CUE = 0.8  # 每条字幕至少显示这么久（秒）
TAIL_HOLD = 0.12  # 说完后字幕多停留的时间，避免和最后一个字同时消失
GAP_BREAK = 0.7  # 输出时间里两个词相隔超过这个值就另起一条
SEAM_BREAK = 1.0  # 剪口两侧素材时间相隔超过这个值，另起一条（句子被剪成两半，不硬连）
TINY_CHARS = 3  # 不超过这个长度又不够 MIN_CUE 的字幕，并进相邻一条

_SENT_END = set("。！？!?；;…")
_COMMA = set("，、：:,")
_PUNCT = _SENT_END | _COMMA | set("“”\"'（）()《》—-·.")
_KEEP_TAIL = set("？！?!…")  # 行尾保留的标点（其余标点在短视频字幕里不显示）


@dataclass(frozen=True)
class Cue:
    start: float  # 输出时间（秒）
    end: float
    text: str


@dataclass
class _Tok:
    text: str
    start: float  # 输出时间
    end: float
    punct: bool
    src: str  # 素材 id
    t0: float  # 素材时间，用来判断剪口两侧是不是同一句话
    t1: float


# ------------------------------------------------------------------ 文字稿读取


def load_words(plan: Plan, source_id: str) -> list[dict[str, Any]] | None:
    """读素材的逐词文字稿：优先 Source.words（JSON 路径），否则走 Media.words()（有缓存，没有识别模型时为 None）。"""
    src = plan.sources[source_id]
    if src.words:
        p = Path(src.words)
        if not p.is_absolute() and plan.path is not None:
            p = plan.path.parent / p
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("读不了 sources.%s.words=%s：%s；改用语音识别结果", source_id, p, e)
        else:
            return data["words"] if isinstance(data, dict) else data
    if src.kind == "image":
        return None
    try:
        from autocut.analysis import Media

        return Media(plan.source_path(source_id)).words()
    except Exception as e:  # noqa: BLE001 —— 识别失败只是没有字幕，不应让出片失败
        log.warning("素材 %s 取文字稿失败：%s", source_id, e)
        return None


# ------------------------------------------------------------------ 映射到输出时间


def _is_punct(text: str) -> bool:
    return bool(text) and all(c in _PUNCT or c.isspace() for c in text)


def _spans(plan: Plan, output: Output) -> list[tuple[str, float, float, float, float, float]]:
    """(素材, 素材起, 素材止, 输出起, 输出止, 速度)：词能落在输出时间轴上的所有区间。"""
    want = output.captions.source
    spans = []
    for seg, o0, o1 in output.timeline():
        if want is not None and seg.source != want:
            continue
        if seg.mute or seg.reverse or plan.sources[seg.source].kind == "image":
            continue  # 静音或倒放的片段里没有可读的话
        spans.append((seg.source, seg.start, seg.end, o0, o1, seg.speed))
    if want is not None:  # 配音（narration bed）的文字稿跟着 bed 走
        total = output.duration
        for b in output.beds:
            if b.source != want:
                continue
            end = b.end if b.end is not None else (plan.sources[b.source].duration or b.start)
            spans.append((b.source, b.start, end, b.at, min(b.at + (end - b.start), total), 1.0))
    return sorted(spans, key=lambda s: s[3])


def _tokens(plan: Plan, output: Output) -> list[_Tok]:
    spans = _spans(plan, output)
    words_cache: dict[str, list[dict[str, Any]] | None] = {}
    toks: list[_Tok] = []
    for sid, s0, s1, o0, o1, speed in spans:
        if sid not in words_cache:
            words_cache[sid] = load_words(plan, sid)
        words = words_cache[sid]
        if not words:
            continue
        span_toks: list[_Tok] = []
        for w in words:
            ws, we = float(w["start"]), float(w["end"])
            text = str(w["text"])
            mid = (ws + we) / 2
            if not (s0 - 1e-3 <= mid <= s1 + 1e-3):
                continue  # 被剪掉的词，或者被剪口劈成两半、重心在外面的词
            start = o0 + (max(ws, s0) - s0) / speed
            end = min(o0 + (min(we, s1) - s0) / speed, o1)
            punct = _is_punct(text)
            if start >= o1 - 0.01 and not punct:
                continue  # 超出 slot 被截掉（句末标点紧贴片段结尾，要留着断句）
            span_toks.append(_Tok(text, start, max(end, start), punct, sid, ws, we))
        while span_toks and span_toks[0].punct:  # 剪口劈掉了前一个词，留下的孤零零标点不要
            span_toks.pop(0)
        toks.extend(span_toks)
    return toks


# ------------------------------------------------------------------ 断行


def _wlen(text: str) -> float:
    """显示宽度：中日韩字符 1，其他 0.5（max_chars 对中文是字数，英文按 2 倍计）。"""
    return sum(1.0 if ord(c) >= 0x2E80 else 0.5 for c in text)


def _is_cjk(c: str) -> bool:
    return ord(c) >= 0x2E80


def _render(toks: list[_Tok]) -> str:
    """把词拼成一行显示文字：中文不加空格，英文词之间加空格；行内逗号变空格，行首尾标点去掉（问号感叹号保留）。"""
    out = ""
    for t in toks:
        x = t.text.strip()
        if not x:
            continue
        if out and not t.punct and not _is_cjk(out[-1]) and not _is_cjk(x[0]) and out[-1] not in "(（“\"":
            out += " "
        out += x
    out = re.sub(r"\s+", " ", re.sub(r"[，、：]", " ", out)).strip()
    out = out.lstrip("".join(_COMMA | set("。；;.")) + " ")
    drop = _PUNCT - _KEEP_TAIL
    while out and (out[-1] in drop or out[-1] == " "):
        out = out[:-1]
    return out


def _clauses(toks: list[_Tok]) -> list[list[_Tok]]:
    """按句末标点、长停顿、剪口切成一句一组。"""
    groups: list[list[_Tok]] = []
    cur: list[_Tok] = []
    for t in toks:
        if cur:
            prev = next((x for x in reversed(cur) if not x.punct), cur[-1])
            if not t.punct:
                gap = t.start - prev.end
                seam = prev.src != t.src or t.t0 < prev.t0 or (t.t0 - prev.t1) > SEAM_BREAK
                if gap > GAP_BREAK or seam:
                    groups.append(cur)
                    cur = []
        cur.append(t)
        if t.punct and t.text.strip()[-1:] in _SENT_END:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [g for g in groups if any(not t.punct for t in g)]


def _split_clause(clause: list[_Tok], max_chars: float) -> list[list[_Tok]]:
    """一句太长就拆成几行。动态规划选断点：每行不超过 max_chars，行长尽量均匀（避免最后一行只剩一两个字），断在逗号后更优先。"""
    if _wlen(_render(clause)) <= max_chars:
        return [clause]
    m = len(clause)
    inf = float("inf")
    best, back = [inf] * (m + 1), [0] * (m + 1)
    best[0] = 0.0
    for j in range(1, m + 1):
        for i in range(j - 1, -1, -1):
            w = _wlen(_render(clause[i:j]))
            if w > max_chars and j - i > 1:
                break  # 再往左只会更长
            cost = best[i] + (max_chars - min(w, max_chars)) ** 2 + 4.0  # 每多一行加一点代价，避免无谓拆分
            if j < m and clause[j - 1].punct and clause[j - 1].text.strip()[-1:] in _COMMA:
                cost -= 8.0  # 断在逗号后更自然
            if cost < best[j]:
                best[j], back[j] = cost, i
    lines, j = [], m
    while j > 0:
        lines.append(clause[back[j] : j])
        j = back[j]
    return lines[::-1]


def cues_for(plan: Plan, output: Output) -> list[Cue]:
    """输出时间轴上的字幕。没有文字稿（或词都被剪掉了）时返回空列表，由调用方决定怎么提示。"""
    max_chars = max(4, output.captions.max_chars)
    toks = _tokens(plan, output)
    if not toks:
        return []
    rows: list[list[Any]] = []  # [start, end, text, wlen]
    for clause in _clauses(toks):
        for line in _split_clause(clause, max_chars):
            words = [t for t in line if not t.punct]
            if not words:
                continue
            text = _render(line)
            if text:
                rows.append([words[0].start, words[-1].end, text, _wlen(text)])
    # 太短的一条（"嗯"、"好"）并进相邻一条，别一闪而过
    i = 0
    while i < len(rows):
        s, e, text, w = rows[i]
        if e - s < MIN_CUE and w <= TINY_CHARS:
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            prv = rows[i - 1] if i > 0 else None
            if nxt and nxt[0] - e < 0.5 and _wlen(text + " " + nxt[2]) <= max_chars:
                nxt[0], nxt[2] = s, f"{text} {nxt[2]}"
                nxt[3] = _wlen(nxt[2])
                rows.pop(i)
                continue
            if prv and s - prv[1] < 0.5 and _wlen(prv[2] + " " + text) <= max_chars:
                prv[1], prv[2] = e, f"{prv[2]} {text}"
                prv[3] = _wlen(prv[2])
                rows.pop(i)
                continue
        i += 1
    total = output.duration
    cues: list[Cue] = []
    for j, (s, e, text, _) in enumerate(rows):
        nxt_start = rows[j + 1][0] if j + 1 < len(rows) else total
        end = max(e + TAIL_HOLD, s + MIN_CUE)
        end = min(end, nxt_start, total)
        cues.append(Cue(round(s, 3), round(max(end, s + 0.05), 3), text))
    log.info("字幕 %s：%d 个词 → %d 条（max_chars=%d）", output.id, len(toks), len(cues), max_chars)
    return cues


# ------------------------------------------------------------------ SRT


def _ts(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(cues: list[Cue]) -> str:
    return "".join(f"{i}\n{_ts(c.start)} --> {_ts(c.end)}\n{c.text}\n\n" for i, c in enumerate(cues, 1))


def write_srt(cues: list[Cue], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(format_srt(cues), encoding="utf-8")
    log.info("字幕已写入 %s（%d 条）", p, len(cues))
    return p


_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def parse_srt(text: str) -> list[Cue]:
    """读 SRT（体检和测试用）。容忍 BOM、CRLF、序号缺失。"""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").lstrip("﻿").strip()):
        lines = block.split("\n")
        for k, ln in enumerate(lines):
            m = _SRT_TIME.search(ln)
            if m:
                g = m.groups()
                a = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")) / 1000
                b = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")) / 1000
                cues.append(Cue(a, b, "\n".join(lines[k + 1 :]).strip()))
                break
    return cues
