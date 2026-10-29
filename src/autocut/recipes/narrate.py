"""文稿 + 配图 → 知识视频（"narrate"配方）。

流程：
1. 文稿按句切开，逐句用 macOS `say` 合成配音，记下每句的精确起止，拼成一条配音 wav（放在缓存目录）。
2. 每句的时间写成"一句一项"的逐词文字稿 JSON，挂在配音 Source 的 words 上，字幕从它生成。
3. 图片 / 视频片段按顺序分给各句：每个素材至少停留 min_hold 秒，各自停留的时间尽量接近。
   文稿里写 `![说明](img2.png)` 可以指定"从这句起用这张图"，每个素材都指定时按指定的来。
4. 第一句是问题（或文稿有标题）时，把它作为大字叠加在开头 2–3 秒（F2：开头决定观众走不走）。

方案里不渲染任何东西：配音、字幕、图片都只是引用，渲染由 render.py 完成。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autocut.analysis import Media, cache_root
from autocut.plan import Bed, Captions, Output, Overlay, Plan, Segment, Source, new_segment_id
from autocut.recipes._common import RecipeError, finalize, make_source, plan_path_for, stored_path, unique_id

log = logging.getLogger(__name__)

SCRIPT_EXT = {".md", ".markdown", ".txt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

TTS_SR = 24000
MIN_HOLD = 2.5  # 每个素材至少停留这么久（S72：图表停留太短，新人跟不上）
GAP = 0.25  # 句间停顿
GAP_PARAGRAPH = 0.3  # 段末额外停顿
GAP_QUESTION = 0.15  # 问句后额外停顿（给观众一拍想一想）
LEAD = 0.15  # 配音开头的静音
TAIL = 0.5  # 最后一句之后画面多停留的时间
HOOK_RANGE = (2.0, 3.0)  # 开头钩子的显示时长范围
_TRIM_DB = -48.0  # 裁掉 say 输出首尾低于此电平的静音
_TRIM_PAD = 0.03
_QUESTION_START = re.compile(r"^(为什么|为何|怎么|怎样|如何|什么是|是什么|为啥|why|how|what|who|when|where|is |are |do |does |can )", re.I)
_END_CHARS = "。！？!?…"
_CLOSERS = "”’」』）)\"'】"
_ABBR = {"mr", "mrs", "ms", "dr", "prof", "vs", "etc", "e.g", "i.e", "st", "no", "fig"}
_IMG_DIRECTIVE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)")


@dataclass
class Script:
    title: str | None
    sentences: list[str]
    paragraph_end: list[bool]
    image_at: dict[str, int]  # 指定的素材文件名 → 从第几句（0 起）开始用


# ------------------------------------------------------------------ 文稿解析


def parse_script(text: str) -> Script:
    """Markdown / 纯文本 → 句子列表。标题行（# …）不朗读，只当标题；`![](x.png)` 是配图指令，不朗读。"""
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)  # YAML front matter
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    title: str | None = None
    sentences: list[str] = []
    para_end: list[bool] = []
    image_at: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if para_end:
                para_end[-1] = True
            continue
        if line.startswith("#"):
            if title is None:
                title = _clean_inline(line.lstrip("#"))
            if para_end:
                para_end[-1] = True
            continue
        for m in _IMG_DIRECTIVE.finditer(line):
            image_at[Path(m.group(1)).name] = len(sentences)
        line = _IMG_DIRECTIVE.sub("", line)
        line = re.sub(r"^(\s*[-*+]\s+|\s*\d+[.)]\s+|>\s*)", "", line)
        line = _clean_inline(line)
        if not line:
            continue
        for s in _split_sentences(line):
            sentences.append(s)
            para_end.append(False)
    if para_end:
        para_end[-1] = True
    return Script(title=title, sentences=sentences, paragraph_end=para_end, image_at=image_at)


def _clean_inline(s: str) -> str:
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)  # 链接只留文字
    s = re.sub(r"[*_`~]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _split_sentences(line: str) -> list[str]:
    out: list[str] = []
    cur = ""
    i = 0
    while i < len(line):
        c = line[i]
        cur += c
        end = False
        if c in _END_CHARS:
            end = True
        elif c == "." and (i + 1 == len(line) or line[i + 1].isspace()):
            word = re.findall(r"[A-Za-z.]+$", cur[:-1])
            end = not (word and word[0].lower().strip(".") in _ABBR)
        if end:
            while i + 1 < len(line) and (line[i + 1] in _END_CHARS or line[i + 1] in _CLOSERS):
                i += 1
                cur += line[i]
            out.append(cur.strip())
            cur = ""
        i += 1
    if cur.strip():
        out.append(cur.strip())
    return [s for s in out if re.sub(r"[\W_]+", "", s)]


def detect_voice(sentences: list[str]) -> str:
    text = "".join(sentences)
    cjk = sum(1 for c in text if ord(c) >= 0x2E80)
    return "Tingting" if text and cjk / len(text) > 0.3 else "Samantha"


# ------------------------------------------------------------------ 配音


def _say(voice: str, rate: int | None, text: str, dst: Path) -> None:
    txt = dst.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    cmd = ["say", "-v", voice, "-f", str(txt), "-o", str(dst), "--file-format=WAVE", f"--data-format=LEI16@{TTS_SR}"]
    if rate:
        cmd[1:1] = ["-r", str(rate)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RecipeError("找不到 macOS 的 `say` 命令：narrate 用它合成配音，只能在 macOS 上运行") from None
    if r.returncode != 0:
        avail = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout.splitlines()
        names = [ln.split("  ")[0].strip() for ln in avail[:8]]
        raise RecipeError(f"say 合成失败（voice={voice!r}）：{r.stderr.strip()[-200:] or '没有输出'}。"
                          f"可用声音例如 {names}；用 voice='名字' 指定，系统设置 → 辅助功能 → 朗读内容 里可以下载更多")


def _read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise RecipeError(f"say 输出的格式不符合预期（{w.getnchannels()} 声道 {w.getsampwidth() * 8} 位）：{p}")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def _trim(x: np.ndarray) -> np.ndarray:
    above = np.flatnonzero(np.abs(x) > 10 ** (_TRIM_DB / 20))
    if above.size == 0:
        return x[: int(0.2 * TTS_SR)]
    pad = int(_TRIM_PAD * TTS_SR)
    return x[max(above[0] - pad, 0) : min(above[-1] + pad + 1, len(x))]


def _write_wav(p: Path, x: np.ndarray) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TTS_SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def synthesize(script: Script, voice: str, rate: int | None, workdir: Path) -> tuple[Path, Path, list[dict]]:
    """逐句合成并拼接。返回 (配音 wav, 逐句文字稿 JSON, 文字稿内容)。结果按 文稿+声音+语速 缓存。"""
    key = hashlib.sha1(json.dumps([script.sentences, script.paragraph_end, voice, rate, TTS_SR], ensure_ascii=False).encode()).hexdigest()[:12]
    d = workdir / f"narrate-{key}"
    wav_p, words_p = d / "narration.wav", d / "narration.words.json"
    if wav_p.exists() and words_p.exists():
        try:
            words = json.loads(words_p.read_text())
            log.info("配音命中缓存：%s", d)
            return wav_p, words_p, words
        except json.JSONDecodeError:
            log.warning("配音缓存损坏，重新合成：%s", words_p)
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    parts: list[np.ndarray] = [np.zeros(int(LEAD * TTS_SR), np.float32)]
    words: list[dict] = []
    cursor = LEAD
    for i, (s, pe) in enumerate(zip(script.sentences, script.paragraph_end)):
        raw = d / f"s{i:03d}.wav"
        _say(voice, rate, s, raw)
        x = _trim(_read_wav(raw))
        dur = len(x) / TTS_SR
        words.append({"text": s, "start": round(cursor, 3), "end": round(cursor + dur, 3), "p": 1.0})
        gap = GAP + (GAP_PARAGRAPH if pe else 0.0) + (GAP_QUESTION if s.rstrip(_CLOSERS).endswith(("？", "?")) else 0.0)
        if i == len(script.sentences) - 1:
            gap = TAIL
        parts += [x, np.zeros(int(gap * TTS_SR), np.float32)]
        cursor += dur + gap
        raw.unlink(missing_ok=True)
        raw.with_suffix(".txt").unlink(missing_ok=True)
    _write_wav(wav_p, np.concatenate(parts))
    words_p.write_text(json.dumps(words, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log.info("配音合成：%d 句，%.1fs，voice=%s rate=%s，耗时 %.2fs → %s", len(words), cursor, voice, rate, time.perf_counter() - t0, wav_p)
    return wav_p, words_p, words


# ------------------------------------------------------------------ 分配配图


def partition(bounds: list[float], k: int, min_hold: float) -> list[int] | None:
    """把 N 句分成 k 组连续的句子，使各组时长尽量接近，且每组 ≥ min_hold。

    bounds：长度 N+1，bounds[i] 是第 i 句开始的时刻（bounds[N] = 总时长）。
    返回各组的起始句序号（长度 k）；做不到返回 None。动态规划，最小化 Σ(组时长 - 平均)²。
    """
    n = len(bounds) - 1
    if k < 1 or k > n:
        return None
    target = (bounds[n] - bounds[0]) / k
    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    cost[0][0] = 0.0
    for g in range(1, k + 1):
        for j in range(g, n + 1):
            for i in range(g - 1, j):
                d = bounds[j] - bounds[i]
                if d < min_hold - 1e-9 or cost[g - 1][i] == inf:
                    continue
                c = cost[g - 1][i] + (d - target) ** 2
                if c < cost[g][j]:
                    cost[g][j], back[g][j] = c, i
    if cost[k][n] == inf:
        return None
    starts, j = [], n
    for g in range(k, 0, -1):
        j = back[g][j]
        starts.append(j)
    return starts[::-1]


def _plural_range(a: int, b: int) -> str:
    return f"第 {a + 1} 句" if a == b else f"第 {a + 1}–{b + 1} 句"


# ------------------------------------------------------------------ 开头钩子


def hook_for(script: Script) -> tuple[str, str] | None:
    """(钩子文字, 来源说明)。第一句是问句就用它；否则用文稿标题；都没有返回 None。"""
    if not script.sentences:
        return None
    first = script.sentences[0]
    if first.rstrip(_CLOSERS).endswith(("？", "?")) or _QUESTION_START.match(first):
        return first, "第一句是个问题"
    if script.title:
        return script.title, "文稿标题"
    return None


# ------------------------------------------------------------------ 配方入口


def build(
    sources: list[Path],
    *,
    out: Path | None = None,
    voice: str | None = None,
    size: tuple[int, int] = (1920, 1080),
    music: Path | str | None = None,
    rate: int | None = None,
) -> Plan:
    """文稿 + 配图 → 知识视频方案。

    sources：一个 .md/.txt 文稿 + 若干图片（或视频片段），素材按给定顺序依次使用；
    voice：say 的声音名，默认按文稿语言选 Tingting / Samantha；size：成片尺寸；
    music：可选 BGM（配音时压低 12dB）；rate：say 的语速（词/分钟）。
    """
    t_start = time.perf_counter()
    paths = [Path(p) for p in sources]
    scripts = [p for p in paths if p.suffix.lower() in SCRIPT_EXT]
    visuals = [p for p in paths if p.suffix.lower() in IMAGE_EXT | VIDEO_EXT]
    unknown = [p for p in paths if p not in scripts and p not in visuals]
    if len(scripts) != 1:
        raise RecipeError(f"narrate 需要恰好一个文稿（.md / .txt），收到 {len(scripts)} 个：{[p.name for p in scripts]}；"
                          "sources 里放 1 个文稿 + 若干图片，例如 [script.md, img1.png, img2.png]")
    if unknown:
        raise RecipeError(f"不认识这些素材的类型：{[p.name for p in unknown]}；支持文稿 {sorted(SCRIPT_EXT)}、图片 {sorted(IMAGE_EXT)}、视频 {sorted(VIDEO_EXT)}")
    if not visuals:
        raise RecipeError("narrate 需要至少一张图片或一段视频来铺画面；把它们加进 sources")
    for p in paths:
        if not p.exists():
            raise RecipeError(f"找不到素材 {p}")
    script_path = scripts[0]
    script = parse_script(script_path.read_text(encoding="utf-8"))
    if not script.sentences:
        raise RecipeError(f"{script_path.name} 里没有可朗读的句子（标题行和图片指令不朗读）；写几句正文再试")
    notes: list[str] = []
    voice_name = voice or detect_voice(script.sentences)
    log.info("narrate：%s，%d 句，%d 个画面素材，voice=%s", script_path.name, len(script.sentences), len(visuals), voice_name)

    # ---- 配音与逐句文字稿
    wav_p, words_p, words = synthesize(script, voice_name, rate, cache_root(script_path))
    narr_media = Media(wav_p)
    narr_dur = narr_media.duration

    # ---- 素材登记
    plan_path = plan_path_for(script_path, out)
    srcs: dict[str, Source] = {}
    narr = make_source("narration", narr_media, plan_path, kind="audio")
    narr.words = stored_path(words_p, plan_path)
    srcs["narration"] = narr
    vis_ids: list[str] = []
    vis_media: dict[str, Media] = {}
    for p in visuals:
        sid = unique_id(p.stem, set(srcs) | {"music"})
        m = Media(p)
        if m.info.video is None:
            raise RecipeError(f"{p} 不是图片或视频（没有画面），不能当配图")
        kind = "image" if p.suffix.lower() in IMAGE_EXT else "video"
        s = make_source(sid, m, plan_path, kind=kind)
        if kind == "image":
            s.duration = None
        srcs[sid] = s
        vis_ids.append(sid)
        vis_media[sid] = m

    # ---- 句子时间 → 分组
    n = len(words)
    total = narr_dur
    starts_s = [float(w["start"]) for w in words]
    ends_s = [float(w["end"]) for w in words]
    bounds = [0.0] + [(ends_s[i] + starts_s[i + 1]) / 2 for i in range(n - 1)] + [total]
    by_name = {p.name: sid for p, sid in zip(visuals, vis_ids)}
    directed = {by_name[name]: idx for name, idx in script.image_at.items() if name in by_name}
    missing = [name for name in script.image_at if name not in by_name]
    if missing:
        notes.append(f"文稿里指定了 {missing}，但 sources 里没有这些文件，已忽略这些指令")

    use_ids = list(vis_ids)
    starts: list[int] | None = None
    basis = "按顺序均分"
    if directed and len(directed) == len(vis_ids):
        order = sorted(vis_ids, key=lambda v: directed[v])
        idxs = [directed[v] for v in order]
        if idxs[0] != 0:
            idxs[0] = 0  # 第一句之前没有画面可用：第一张图从头开始
        durs = [bounds[b] - bounds[a] for a, b in zip(idxs, idxs[1:] + [n])]
        if len(set(idxs)) == len(idxs) and min(durs) >= MIN_HOLD - 1e-9:
            use_ids, starts, basis = order, idxs, "按文稿里的指定"
        else:
            notes.append("文稿里的配图指定位置重复或会让某张图停留不足 %.1fs，改为按顺序均分" % MIN_HOLD)
    elif directed:
        notes.append("文稿只给部分配图指定了位置（要么全部指定，要么都不指定），改为按顺序均分")
    if starts is None:
        k = min(len(vis_ids), n)
        while k >= 1:
            starts = partition(bounds, k, MIN_HOLD)
            if starts is not None:
                break
            k -= 1
        if starts is None:  # 整个视频都不到 MIN_HOLD
            starts, k = [0], 1
        if k < len(vis_ids):
            dropped = [s for s in vis_ids[k:]]
            use_ids = vis_ids[:k]
            notes.append(f"配音一共 {total:.1f}s，每个素材至少停留 {MIN_HOLD}s 放不下 {len(vis_ids)} 个，只用了前 {k} 个；"
                         f"没用上：{dropped}。加长文稿或减少配图")

    # ---- 主轨：图片 / 视频片段
    segs: list[Segment] = []
    bounds_g = [bounds[s] for s in starts] + [total]
    sentence_idx = starts + [n]
    for g, sid in enumerate(use_ids):
        a, b = sentence_idx[g], sentence_idx[g + 1] - 1
        slot = bounds_g[g + 1] - bounds_g[g]
        src = srcs[sid]
        first = script.sentences[a]
        gist = first if len(first) <= 24 else first[:23] + "…"
        text = " ".join(script.sentences[a : b + 1])
        if src.kind == "image":
            segs.append(Segment(
                id=new_segment_id("n", g + 1), source=sid, start=0.0, end=round(slot, 3), reason="image",
                confidence=0.9 if basis == "按文稿里的指定" else 0.6,
                detail=f"{_plural_range(a, b)}：{gist}（{basis}；缓慢推拉）", text=text, slot=round(slot, 3),
                evidence={"motion": "kenburns", "sentences": [a + 1, b + 1], "assigned": basis},
            ))
        else:
            vdur = src.duration or slot
            segs.append(Segment(
                id=new_segment_id("n", g + 1), source=sid, start=0.0, end=round(min(vdur, slot), 3), reason="shot",
                confidence=0.9 if basis == "按文稿里的指定" else 0.6, mute=True,
                detail=f"{_plural_range(a, b)}：{gist}（{basis}；视频片段，原声静音）" + ("；片段比配音短，定格补齐" if vdur < slot - 0.05 else ""),
                text=text, slot=round(slot, 3), evidence={"sentences": [a + 1, b + 1], "assigned": basis},
            ))
    if basis == "按顺序均分":
        notes.append("配图是按顺序均分的：工具不知道图里画了什么，请核对每张图和它对应的句子是否匹配"
                     "（也可以在文稿里用 ![说明](文件名) 指定从哪句开始用哪张图）")

    # ---- 叠加层：开头钩子
    overlays: list[Overlay] = []
    hook = hook_for(script)
    if hook:
        text, why = hook
        dur_hook = min(max(ends_s[0] + 0.4, HOOK_RANGE[0]), HOOK_RANGE[1])
        overlays.append(Overlay(kind="text", at=0.0, duration=round(dur_hook, 2), text=text, position="center"))
        notes.append(f"开头钩子：「{text}」大字显示 {dur_hook:.1f}s（{why}）")
    else:
        notes.append("第一句不是问题，文稿也没有标题，所以开头没有大字钩子；建议第一句抛出问题或结论")

    # ---- BGM
    beds = [Bed(source="narration", at=0.0, start=0.0, end=round(narr_dur, 3), role="narration")]
    if music is not None:
        mp = Path(music)
        if not mp.exists():
            raise RecipeError(f"找不到 BGM 文件 {mp}")
        mm = Media(mp)
        srcs["music"] = make_source("music", mm, plan_path, kind="audio")
        m_end = min(mm.duration, total)
        beds.append(Bed(source="music", at=0.0, start=0.0, end=round(m_end, 3), gain_db=-6.0, duck_db=12.0, role="music"))
        if mm.duration < total - 0.5:
            notes.append(f"BGM 只有 {mm.duration:.1f}s，比成片（{total:.1f}s）短，后半段没有背景音乐；换一首长的，或在剪辑软件里循环")

    title = script.title or script.sentences[0]
    out_obj = Output(
        id="explainer", segments=segs, width=int(size[0]), height=int(size[1]), beds=beds, overlays=overlays,
        captions=Captions(enabled=True, burn=True, source="narration"), title=title,
        reasons=[f"{n} 句配音 {narr_dur:.1f}s（{voice_name}）", f"{len(segs)} 个画面，每个至少停留 {MIN_HOLD}s"],
    )
    plan = Plan(
        recipe="narrate", sources=srcs, outputs=[out_obj],
        options={"script": stored_path(script_path, plan_path), "voice": voice_name, "size": [int(size[0]), int(size[1])],
                 "rate": rate, "music": str(music) if music else None, "min_hold": MIN_HOLD, "sentences": n},
        notes=notes, path=plan_path,
    )
    finalize(plan)
    log.info("narrate 完成：%d 句 / %d 个画面 / %.1fs，总耗时 %.2fs", n, len(segs), out_obj.duration, time.perf_counter() - t_start)
    return plan
