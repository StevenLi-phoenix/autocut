"""语音识别：本机 whisper.cpp（whisper-cli）→ 逐词文字稿。

产出格式见 docs/dev/architecture.md「逐词文字稿」：
    [{"text": "大家好", "start": 0.12, "end": 0.86, "p": 0.98}, {"text": "，", "start": 0.86, "end": 0.86, "p": 0.97}]

时间怎么来（实测结论见 docs/dev/asr-whisper-cpp.md）：
- whisper 只负责"说了什么、词序如何"；每个 token 的 `t_dtw`（10ms 为单位）是它的**结束**时间，系统性偏早约 150ms。
- 停顿之后的句首拿不到起点（静音被算进了前一个标点），所以边界以音频为准：用细粒度语音检测
  （停顿 ≥ SNAP_MIN_SILENCE 就断开）得到语音段，每个词按结束时间归到一个语音段；
  段内第一个词从段起点开始，最后一个词到段终点结束，中间的词首尾相接。
- 结束时间附近没有任何语音能量的词丢掉：那是 whisper 在空白处编出来的文字（traces S34）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from bisect import bisect_right
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autocut.audio import resolve_track

if TYPE_CHECKING:
    from autocut.analysis import Media

log = logging.getLogger(__name__)

ENV_MODEL = "AUTOCUT_WHISPER_MODEL"  # 模型文件路径（或放模型的目录）
ENV_CLI = "AUTOCUT_WHISPER_CLI"  # whisper-cli 可执行文件路径
REPO_MODELS = Path(__file__).resolve().parents[2] / "models"
USER_MODELS = Path.home() / ".cache" / "autocut" / "models"

# 同一目录里有多个模型时的优先顺序：只有 f16 的 large-v3-turbo 的 DTW 词级时间戳实测可用
_PREFERRED = (
    "ggml-large-v3-turbo.bin",
    "ggml-large-v3.bin",
    "ggml-large-v3-turbo-q8_0.bin",
    "ggml-large-v3-turbo-q5_0.bin",
    "ggml-medium.bin",
    "ggml-small.bin",
    "ggml-base.bin",
    "ggml-tiny.bin",
)
# whisper-cli -dtw 接受的预设名
_DTW_PRESETS = frozenset({
    "tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
    "large.v1", "large.v2", "large.v3", "large.v3.turbo",
})
# 小模型中文默认输出繁体，加这句提示改成简体（turbo 不需要）
_ZH_PROMPT = "以下是普通话的句子。"
_SMALL_MODELS = ("tiny", "base", "small")

SR = 16000  # whisper 只吃 16 kHz
# 解析与词边界吸附逻辑的版本：改了 words_from_whisper 或下面的吸附参数就加 1，analysis 的文字稿缓存随之失效
PARSE_VERSION = 1
# 词边界吸附用的语音检测：停顿短于此并入语音（字间的塞音闭塞一般更短）。cut 配方用同一份结果
SNAP_MIN_SILENCE = 0.15
# 词的结束时间落在静音里时：下一个语音段在这么近的地方开始，就归给它（t_dtw 偏早约 150ms，最差 270ms）
_EARLY_TOL = 0.4
# 或者上一个语音段刚结束不久，就归给上一个
_LATE_TOL = 0.25

# 中文后面的半角标点换成全角（whisper 中文输出的是半角）
_FULLWIDTH = {",": "，", ".": "。", "?": "？", "!": "！", ":": "：", ";": "；"}


class TranscribeError(RuntimeError):
    """语音识别失败：消息说明哪一步、哪个文件、怎么修。"""


# ------------------------------------------------------------------ 模型与可执行文件


def model_search_paths() -> list[Path]:
    """按顺序查找模型的位置（给人看的说明里也用它）。"""
    out = []
    env = os.environ.get(ENV_MODEL)
    if env:
        out.append(Path(env).expanduser())
    return out + [REPO_MODELS, USER_MODELS]


def _pick(d: Path) -> Path | None:
    if not d.is_dir():
        return None
    for name in _PREFERRED:
        p = d / name
        if p.is_file():
            return p
    rest = [p for p in d.glob("ggml-*.bin") if "vad" not in p.name.lower()]
    return max(rest, key=lambda p: p.stat().st_size) if rest else None


def find_model() -> Path | None:
    """环境变量 AUTOCUT_WHISPER_MODEL → 仓库 models/ → ~/.cache/autocut/models/；同一目录内优先 f16 的 large-v3-turbo。"""
    env = os.environ.get(ENV_MODEL)
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p.resolve()
        if p.is_dir() and (m := _pick(p)):
            return m.resolve()
        log.warning("%s=%s 不是模型文件，也不是含 ggml-*.bin 的目录；继续在默认位置查找", ENV_MODEL, env)
    for d in (REPO_MODELS, USER_MODELS):
        if m := _pick(d):
            return m.resolve()
    return None


def whisper_cli() -> str | None:
    env = os.environ.get(ENV_CLI)
    if env:
        return env if Path(env).is_file() else None
    found = shutil.which("whisper-cli")
    if found:
        return found
    brew = Path("/opt/homebrew/bin/whisper-cli")
    return str(brew) if brew.is_file() else None


def available() -> bool:
    return whisper_cli() is not None and find_model() is not None


def unavailable_reason() -> str | None:
    """不可用时给人看的原因（含查找过的路径和修法）；可用时返回 None。"""
    parts = []
    if whisper_cli() is None:
        parts.append(f"没有找到 whisper-cli（查找过 ${ENV_CLI}、PATH、/opt/homebrew/bin；macOS 可 brew install whisper-cpp）")
    if find_model() is None:
        where = "、".join(str(p) for p in model_search_paths())
        parts.append(f"没有找到语音识别模型（查找过的路径：{where}；把 ggml-large-v3-turbo.bin 放进其中之一，或设置 ${ENV_MODEL}）")
    return "；".join(parts) or None


def _tag(model: Path) -> str:
    name = model.name
    name = name[len("ggml-"):] if name.startswith("ggml-") else name
    return name[: -len(".bin")] if name.endswith(".bin") else name


def model_tag() -> str:
    """当前模型的短名，例如 "large-v3-turbo"；用于缓存键。没有模型时返回 "none"。"""
    m = find_model()
    return _tag(m) if m else "none"


def _dtw_preset(tag: str) -> str | None:
    """"large-v3-turbo-q5_0" → "large.v3.turbo"；认不出返回 None（不开 DTW）。"""
    base = re.sub(r"[-_.]q\d+(_\d+|_k)?$", "", tag)
    preset = base.replace("-", ".")
    return preset if preset in _DTW_PRESETS else None


# ------------------------------------------------------------------ 运行 whisper-cli


def _extract_wav(src: Path, track: int | None, dst: Path) -> None:
    idx = resolve_track(src, track)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-map", f"0:a:{idx}", "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise TranscribeError(f"ffmpeg 抽取音轨 {idx} 失败（{src}）：{r.stderr.strip()[-500:]}")


def _run_whisper(media: Media, cli: str, model: Path, lang: str, track: int | None, raw: Path) -> None:
    """抽 16k 单声道 wav → whisper-cli → 原始 JSON 原子写入 raw（缓存）。"""
    tag = _tag(model)
    preset = _dtw_preset(tag)
    with tempfile.TemporaryDirectory(prefix="autocut-asr-") as td:
        wav, prefix = Path(td) / "audio.wav", Path(td) / "out"
        t0 = time.perf_counter()
        _extract_wav(media.path, track, wav)
        t1 = time.perf_counter()
        cmd = [cli, "-m", str(model), "-l", lang, "-np", "-ojf", "-of", str(prefix), "-f", str(wav)]
        if preset:
            # -dtw 必须配 -nfa：flash attention 开着时 DTW 会被静默关掉，t_dtw 全是 -1
            cmd += ["-nfa", "-dtw", preset]
        else:
            log.warning("模型 %s 没有对应的 DTW 预设，词级时间退回 whisper 的启发式时间（不准）", tag)
        if (preset or "").split(".")[0] in _SMALL_MODELS and lang in ("auto", "zh"):
            cmd += ["--prompt", _ZH_PROMPT]
        timeout = 120 + 4 * media.duration
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise TranscribeError(f"whisper-cli 超过 {timeout:.0f}s 没有结束（{media.path.name}）") from e
        out = prefix.with_suffix(".json")
        if r.returncode != 0 or not out.exists():
            err = r.stderr.decode("utf-8", "replace").strip()[-800:]
            raise TranscribeError(f"whisper-cli 失败（返回码 {r.returncode}，模型 {model}，素材 {media.path.name}）：{err}")
        raw.parent.mkdir(parents=True, exist_ok=True)
        tmp = raw.with_suffix(".tmp")
        shutil.copyfile(out, tmp)
        tmp.replace(raw)
    log.info("whisper-cli %s · %s · -l %s：抽音轨 %.2fs，识别 %.2fs（素材 %.1fs）",
             media.path.name, tag, lang, t1 - t0, time.perf_counter() - t1, media.duration)


def _load_raw(raw: Path) -> dict[str, Any] | None:
    try:
        # whisper.cpp 按字节写 token：一个汉字可能被拆在两个 token 里，用 surrogateescape 保住原始字节
        return json.loads(raw.read_bytes().decode("utf-8", "surrogateescape"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("原始识别结果损坏，重新识别：%s（%s）", raw, e)
        return None


# ------------------------------------------------------------------ 解析


def _is_punct(s: str) -> bool:
    return bool(s) and all(unicodedata.category(c)[0] in "PS" for c in s)


def _is_cjk(c: str) -> bool:
    return "一" <= c <= "鿿" or "㐀" <= c <= "䶿" or "぀" <= c <= "ヿ" or "豈" <= c <= "﫿"


def _tokens(data: dict[str, Any]) -> list[tuple[str, float, float]]:
    """JSON → [(token 原文, 结束时间秒, p)]；去掉 [_BEG_] 等特殊 token，拼回被拆开的 UTF-8 字符。"""
    out: list[tuple[str, float, float]] = []
    pending, pend_p = b"", 1.0
    for seg in data.get("transcription", []):
        for tok in seg.get("tokens", []):
            text = tok.get("text", "")
            if text.startswith("[_") or (text.startswith("<|") and text.endswith("|>")):
                continue
            t_dtw = tok.get("t_dtw", -1)
            end = t_dtw / 100 if t_dtw is not None and t_dtw >= 0 else tok.get("offsets", {}).get("to", 0) / 1000
            p = float(tok.get("p", 1.0))
            buf = pending + text.encode("utf-8", "surrogateescape")
            try:
                s = buf.decode("utf-8")
            except UnicodeDecodeError:
                pending, pend_p = buf, min(pend_p, p)
                continue
            out.append((s, end, min(pend_p, p)))
            pending, pend_p = b"", 1.0
    return out


def _group_words(tokens: Sequence[tuple[str, float, float]]) -> list[dict[str, Any]]:
    """token → 词：英文以空格开头的 token 开始新词（子词合并），中文每个 token 一项，标点单独成项。"""
    words: list[dict[str, Any]] = []
    for text, end, p in tokens:
        s = text.strip()
        if not s:
            continue
        if _is_punct(s):
            words.append({"text": s, "end": end, "p": p, "punct": True})
            continue
        prev = words[-1] if words else None
        new = (prev is None or prev["punct"] or text[0].isspace() or _is_cjk(s[0]) or _is_cjk(prev["text"][-1]))
        if new:
            words.append({"text": s, "end": end, "p": p, "punct": False})
        else:
            prev["text"] += s
            prev["end"] = end
            prev["p"] = min(prev["p"], p)
    return words


def _assign(t: float, regions: Sequence[tuple[float, float]], starts: list[float]) -> int | None:
    """结束时间 t 归哪个语音段；离所有语音段都太远（幻觉）返回 None。"""
    i = bisect_right(starts, t) - 1  # 起点 ≤ t 的最后一个段
    if i >= 0 and t <= regions[i][1]:
        return i
    nxt = i + 1
    if nxt < len(regions) and regions[nxt][0] - t <= _EARLY_TOL:
        return nxt
    if i >= 0 and t - regions[i][1] <= _LATE_TOL:
        return i
    return None


def words_from_whisper(data: dict[str, Any], regions: Sequence[tuple[float, float]]) -> tuple[list[dict[str, Any]], list[str]]:
    """whisper JSON + 语音段 → (逐词文字稿, 被丢弃的词)。纯函数，单元测试直接喂合成数据。"""
    raw = _group_words(_tokens(data))
    regions = [(float(s), float(e)) for s, e in regions]
    starts = [s for s, _ in regions]
    kept: list[tuple[dict[str, Any], int]] = []  # (词, 语音段序号)
    dropped: list[str] = []
    last_ri = 0
    out_items: list[dict[str, Any] | None] = []  # 与 raw 对齐；标点先占位
    prev_dropped = False
    for w in raw:
        if w["punct"]:
            out_items.append(None if prev_dropped or not kept else w)
            continue
        ri = _assign(w["end"], regions, starts) if regions else None
        if ri is None:
            dropped.append(f"{w['text']}@{w['end']:.2f}")
            prev_dropped = True
            out_items.append(None)
            continue
        prev_dropped = False
        ri = max(ri, last_ri)  # t_dtw 偶尔不单调；词序以 whisper 为准
        last_ri = ri
        kept.append((w, ri))
        out_items.append(w)

    # 按语音段吸附起止
    by_region: dict[int, list[dict[str, Any]]] = {}
    for w, ri in kept:
        by_region.setdefault(ri, []).append(w)
    for ri, ws in by_region.items():
        rs, re_ = regions[ri]
        t = rs
        for k, w in enumerate(ws):
            w["start"] = t
            w["end"] = re_ if k == len(ws) - 1 else min(max(w["end"], t), re_)
            t = w["end"]

    # 组装：标点的时间 = 前一个词的结束；中文后面的半角标点换全角
    words: list[dict[str, Any]] = []
    last_word: dict[str, Any] | None = None
    for w in out_items:
        if w is None:
            continue
        if w["punct"]:
            if last_word is None:
                continue
            text = w["text"]
            if text in _FULLWIDTH and _is_cjk(last_word["text"][-1]):
                text = _FULLWIDTH[text]
            t = last_word["end"]
            words.append({"text": text, "start": round(t, 3), "end": round(t, 3), "p": round(w["p"], 3)})
        else:
            last_word = w
            words.append({"text": w["text"], "start": round(w["start"], 3), "end": round(w["end"], 3), "p": round(w["p"], 3)})
    return words, dropped


def transcribe(media: Media, language: str | None = None, track: int | None = None) -> list[dict[str, Any]]:
    """对 media 的 track 音轨做语音识别，返回逐词文字稿（素材时间）。

    language：None / "auto" 自动识别，或 "zh"、"en" 等 whisper 语种代码。
    原始 JSON 先写进 media.cache_dir（whisper-<语种>-t<音轨>-<模型>.json），再解析；解析逻辑变了也不必重跑识别。
    """
    t0 = time.perf_counter()
    cli, model = whisper_cli(), find_model()
    if cli is None or model is None:
        raise TranscribeError(unavailable_reason() or "语音识别不可用")
    tag = _tag(model)
    lang = (language or "auto").strip().lower()
    raw = media.cache_dir / f"whisper-{lang}-t{track}-{tag}.json"
    data = _load_raw(raw) if raw.exists() else None
    if data is None:
        _run_whisper(media, cli, model, lang, track, raw)
        data = _load_raw(raw)
        if data is None:
            raise TranscribeError(f"whisper-cli 的输出不是合法 JSON：{raw}")
    else:
        log.info("复用已缓存的原始识别结果：%s", raw)
    t1 = time.perf_counter()
    sp = media.speech(track, min_silence=SNAP_MIN_SILENCE)
    words, dropped = words_from_whisper(data, sp.regions)
    if dropped:
        log.info("丢弃 %d 个落在没有语音能量处的词（疑似幻觉）：%s", len(dropped), "、".join(dropped[:20]))
    n_words = sum(1 for w in words if not _is_punct(w["text"]))
    log.info("转写 %s %s：%d 词 + %d 标点，识别语种 %s；识别 %.2fs，吸附 %.2fs",
             media.path.name, media.track_label(track), n_words, len(words) - n_words,
             data.get("result", {}).get("language", "?"), t1 - t0, time.perf_counter() - t1)
    return words
