"""发布前体检：对成片（或按方案估算）找出会让观众走掉的信号，每一项都给时间点和可执行的修法。

诚实：这里报的是**留存风险信号**，不是爆款保证（F3：分数只能排序）。没有"爆款分"，也不会有。
阈值是经验值（来源见各常量的注释），不是平台规则；报告里的 message 会写明测到了什么，用户自己判断。

两种目标：
- 成片（mp4 等）：直接分析音视频。
- 方案（Plan）：把各素材的信号按方案映射到输出时间轴上估算（不渲染，几秒出结果）。
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from autocut.audio import frame_db, load_audio
from autocut.loudness import short_term_curve
from autocut.plan import Plan
from autocut.probe import probe
from autocut.speech import detect_speech

log = logging.getLogger(__name__)

DISCLAIMER = "这些是留存风险信号，不是爆款保证。"
SR = 16000
DT = 0.05  # 输出时间轴的网格步长（秒）
DEAD_AIR_MIN = 1.5  # 无人声、很安静的区间超过这么久才报（S88：2 秒没声音没动作会被当成视频结束）
SILENCE_ABS_DB = -35.0  # 非人声且低于这个电平（dBFS，100ms 均方根），并且接近底噪，才算"很安静"
SILENCE_FLOOR_MARGIN = 10.0
DIGITAL_SILENCE_DB = -60.0
STATIC_MOTION = 0.004  # 相邻采样帧的平均绝对差 / 255 低于它算"画面没动"
STATIC_LONG = 8.0  # 画面持续不动超过这么久，提醒加一次画面变化（经验值）
HOOK_WARN = 3.0  # "黄金 3 秒"（S86 经验帖，没有数据支撑）
HOOK_RISK = 6.0
LOUDNESS_TOL = 2.0  # 整合响度偏离目标多少 LU 以内不提醒
TRUE_PEAK_MAX = -1.0  # dBTP；平台转码会再过一遍编码器，真峰值太贴近 0 会削波
MIN_CUE_SHORT = 3.0  # 短于它的成片太短，观众还没反应过来就结束


@dataclass(frozen=True)
class Platform:
    label: str
    orient: str  # vertical | horizontal
    loudness: float  # 目标整合响度（LUFS）
    soft_max: float | None  # 超过它提醒（秒）
    hard_max: float | None  # 超过它报风险
    note: str  # 这些惯例的出处/性质，写进报告里


PLATFORMS: dict[str, Platform] = {
    "douyin": Platform("抖音", "vertical", -14.0, 60.0, 180.0, "信息流短视频惯例 9:16、15–60s 居多，不是硬性规定"),
    "tiktok": Platform("TikTok", "vertical", -14.0, 60.0, 600.0, "9:16，短内容完播率更高（经验）"),
    "shorts": Platform("YouTube Shorts", "vertical", -14.0, 60.0, 180.0, "9:16；Shorts 上限 3 分钟，超过不会作为 Shorts 发布"),
    "reels": Platform("Instagram Reels", "vertical", -14.0, 90.0, 180.0, "9:16，建议 90s 内"),
    "bilibili": Platform("B 站", "horizontal", -14.0, None, None, "主流是 16:9 横屏，竖屏也能发但电脑端两侧留黑"),
    "youtube": Platform("YouTube", "horizontal", -14.0, None, None, "16:9 横屏；响度会被平台归一到约 -14 LUFS"),
}


# ------------------------------------------------------------------ 报告


@dataclass
class CheckItem:
    id: str
    severity: str  # risk | warn | info
    message: str
    fix: str
    at: float | None = None
    end: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity, "at": None if self.at is None else round(self.at, 2),
                "end": None if self.end is None else round(self.end, 2), "message": self.message, "fix": self.fix}


@dataclass
class CheckReport:
    target: str
    platform: str
    duration: float
    metrics: dict[str, Any] = field(default_factory=dict)
    items: list[CheckItem] = field(default_factory=list)
    mode: str = "file"  # file | plan
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def save(self, path: str | Path) -> Path:
        """存档成 JSON（发布前存一份，事后可以和平台的留存曲线对齐）。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        log.info("体检报告已存档：%s", p)
        return p

    @property
    def summary(self) -> str:
        n = {s: sum(1 for i in self.items if i.severity == s) for s in ("risk", "warn", "info")}
        head = f"{n['risk']} 项风险、{n['warn']} 项提醒、{n['info']} 项说明" if self.items else "没有发现明显的留存风险信号"
        scope = "按方案估算" if self.mode == "plan" else "对成片实测"
        return f"{head}（{scope}，目标平台 {PLATFORMS[self.platform].label if self.platform in PLATFORMS else self.platform}）。{DISCLAIMER}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "checked_at": self.checked_at,
            "target": self.target,
            "platform": self.platform,
            "duration": round(self.duration, 2),
            "metrics": self.metrics,
            "items": [i.to_dict() for i in self.items],
            "summary": self.summary,
            "disclaimer": DISCLAIMER,
        }

    def to_text(self) -> str:
        mark = {"risk": "风险", "warn": "提醒", "info": "说明"}
        lines = [f"体检 {self.target}（{self.duration:.1f}s，平台 {self.platform}）", self.summary, ""]
        for i in sorted(self.items, key=lambda x: ({"risk": 0, "warn": 1, "info": 2}[x.severity], x.at or 0)):
            at = "" if i.at is None else f" @{_mmss(i.at)}" + (f"–{_mmss(i.end)}" if i.end is not None else "")
            lines.append(f"[{mark[i.severity]}]{at} {i.message}")
            lines.append(f"    → {i.fix}")
        return "\n".join(lines)


def _mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


# ------------------------------------------------------------------ 信号


@dataclass
class _Tl:
    """输出时间轴（网格 DT）上的信号。"""

    duration: float
    size: tuple[int, int]
    db: np.ndarray  # 响度 dBFS
    voice: np.ndarray  # bool：有人声
    motion: np.ndarray  # 画面变化量，NaN = 没有视频
    cuts: list[float]
    has_video: bool
    has_audio: bool
    words: list[dict[str, Any]] | None = None  # 输出时间的词：{"text","start","end","punct"}
    edge: dict[str, Any] = field(default_factory=dict)  # 开头/结尾的句子完整性判断（有文字稿时）
    words_note: str = ""
    will_normalize: bool = False
    lufs: float | None = None
    true_peak: float | None = None
    lra: float | None = None
    captions: dict[str, Any] = field(default_factory=dict)


def _speech_grid(regions, n: int) -> np.ndarray:
    v = np.zeros(n, dtype=bool)
    for s, e in regions:
        v[int(s / DT) : int(math.ceil(e / DT)) + 1] = True
    return v


def _motion_curve(path: Path, duration: float) -> np.ndarray:
    """每个 DT 格上的画面变化量（0–1）：缩小的灰度帧，相邻采样帧的平均绝对差。长视频降低采样率，上限约 6000 帧。"""
    fps = min(10.0, max(0.5, 6000.0 / max(duration, 1.0)))
    w, h = 64, 36
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an",
           "-vf", f"fps={fps:.4f},scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"读视频帧失败 {path}：{r.stderr.decode('utf-8', 'replace')[-200:]}")
    f = np.frombuffer(r.stdout, dtype=np.uint8)
    f = f[: len(f) // (w * h) * w * h].reshape(-1, h, w).astype(np.int16)
    n = int(math.ceil(duration / DT)) + 1
    if len(f) < 2:
        return np.zeros(n)
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2)) / 255.0  # d[j]：第 j+1 帧相对前一帧
    idx = np.clip((np.arange(n) * DT * fps).astype(int) - 1, 0, len(d) - 1)
    log.debug("motion %s：%d 帧 @%.2ffps，%.0fms", path.name, len(f), fps, (time.perf_counter() - t0) * 1e3)
    return d[idx]


def _ebur128(path: Path) -> tuple[float | None, float | None, float | None]:
    """(整合响度 LUFS, 真峰值 dBTP, LRA)。"""
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-nostats", "-hide_banner", "-v", "info", "-i", str(path), "-vn", "-map", "0:a:0",
         "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    txt = r.stderr
    i = re.findall(r"\bI:\s+(-?\d+(?:\.\d+)?|-?inf)\s+LUFS", txt)
    tp = re.findall(r"Peak:\s+(-?\d+(?:\.\d+)?|-?inf)\s+dBFS", txt)
    lra = re.findall(r"LRA:\s+(-?\d+(?:\.\d+)?)\s+LU", txt)

    def f(x: list[str]) -> float | None:
        return None if not x or "inf" in x[-1] else float(x[-1])

    return f(i), f(tp), f(lra)


def _lufs_np(x: np.ndarray, sr: int) -> float | None:
    """BS.1770 整合响度（400ms 块、-70 LUFS 绝对门限 + 相对 -10 LU 门限）；用来估算方案的响度。"""
    if len(x) < sr // 2:
        return None
    _, db = short_term_curve(x, sr, window=0.4, hop=0.1)
    p = 10 ** (db.astype(np.float64) / 10)
    p = p[db > -70]
    if p.size == 0:
        return None
    rel = -0.691 + 10 * math.log10(p.mean()) - 10
    p = p[(-0.691 + 10 * np.log10(p)) > rel]
    return None if p.size == 0 else float(-0.691 + 10 * math.log10(p.mean()))


def _stream_info(path: Path) -> dict[str, Any]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format_tags=comment", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"subtitle_streams": 0, "comment": ""}
    return {"subtitle_streams": sum(1 for s in d.get("streams", []) if s.get("codec_type") == "subtitle"),
            "comment": (d.get("format", {}).get("tags") or {}).get("comment", "")}


def _file_tl(path: Path, asr: bool = True) -> _Tl:
    t0 = time.perf_counter()
    info = probe(path)
    dur = info.duration
    n = int(math.ceil(dur / DT)) + 1
    has_a, has_v = bool(info.audio), info.video is not None
    db = np.full(n, -100.0)
    voice = np.zeros(n, dtype=bool)
    if has_a:
        x = load_audio(path, sr=SR)
        d = frame_db(x, SR, hop=DT, win=0.1)
        db[: len(d)] = d[:n]
        sp = detect_speech(x, SR)
        voice = _speech_grid(sp.regions, n)
    motion = _motion_curve(path, dur) if has_v else np.full(n, np.nan)
    cuts: list[float] = []
    size = (0, 0)
    if has_v:
        from autocut.render import _display_size
        from autocut.scenes import detect_cuts

        size = _display_size(info)
        try:
            cuts = detect_cuts(path)
        except Exception as e:  # noqa: BLE001 —— 切点检测失败不影响其他指标
            log.warning("切点检测失败：%s", e)
    tl = _Tl(dur, size, db, voice, motion, cuts, has_v, has_a)
    if has_a:
        tl.lufs, tl.true_peak, tl.lra = _ebur128(path)
    st = _stream_info(path)
    sidecars = [p for p in path.parent.glob(f"{path.stem}*.srt") if p.stem == path.stem or p.stem.startswith(path.stem + ".")]
    tl.captions = {"subtitle_streams": st["subtitle_streams"], "sidecar": [p.name for p in sidecars],
                   "burned": "captions=burned" in st["comment"], "known_absent": False}
    try:
        from autocut.analysis import Media

        words = Media(path).words() if asr else None
    except Exception as e:  # noqa: BLE001
        log.warning("取文字稿失败：%s", e)
        words = None
    _attach_words(tl, words, source_words=None)
    log.info("体检信号 %s：%.1fs，%.1fs（%.1fx 实时）", path.name, dur, time.perf_counter() - t0, dur / max(time.perf_counter() - t0, 1e-6))
    return tl


def _attach_words(tl: _Tl, words: list[dict[str, Any]] | None, source_words: dict | None) -> None:
    """文字稿（输出时间）→ 开头/结尾是否落在句中。source_words 非 None 时用素材里的前后文判断。"""
    if not words:
        tl.words_note = "没有文字稿（没有逐词文字稿，也没有可用的语音识别模型）"
        return
    words = [{**w, "punct": bool(w.get("punct", _is_p(str(w["text"])) or w["start"] == w["end"]))} for w in words]
    tl.words = words
    real = [w for w in words if not w["punct"]]
    if not real:
        return
    first, last = real[0], real[-1]
    sent_end = "。！？!?；;…"
    if source_words is not None:
        tl.edge = source_words
    else:
        tail = words[-1]
        tl.edge = {
            "start_mid": None,  # 成片里没有"前文"，开头是否在句中无从判断
            # 成片里只能看"最后一个词后面有没有句末标点"，而且只有话一直说到片尾（1s 内）才算被切断
            "end_mid": (not (tail.get("punct") and tail["text"].strip()[-1:] in sent_end)) and last["end"] >= tl.duration - 1.0,
            "first_text": first["text"], "last_text": last["text"],
            "first_at": first["start"], "last_at": last["end"],
        }


def _plan_tl(plan: Plan, output, asr: bool = True) -> _Tl:
    from autocut.analysis import Media
    from autocut.captions import _tokens, load_words
    from autocut.render import output_size

    t0 = time.perf_counter()
    total = output.duration
    n = int(math.ceil(total / DT)) + 1
    db = np.full(n, -100.0)
    voice = np.zeros(n, dtype=bool)
    motion = np.zeros(n)
    cuts: list[float] = []
    has_v = has_a = False
    medias: dict[str, Media] = {}
    sigs: dict[str, dict[str, Any]] = {}

    def sig(sid: str) -> dict[str, Any]:
        if sid not in sigs:
            s = plan.sources[sid]
            m = medias.setdefault(sid, Media(plan.source_path(sid)))
            info = m.info
            d: dict[str, Any] = {"kind": s.kind, "audio": bool(info.audio), "video": info.video is not None and s.kind == "video", "m": m}
            if info.audio:
                x = m.audio(s.analysis_track, SR)
                d["x"] = x
                d["db"] = frame_db(x, SR, hop=DT, win=0.1)
                d["voice"] = _speech_grid(m.speech(s.analysis_track).regions, len(d["db"]) + 1)
            if d["video"]:
                d["motion"] = _motion_curve(m.path, info.duration)
                try:
                    d["cuts"] = m.cuts()
                except Exception as e:  # noqa: BLE001
                    log.warning("切点检测失败（%s）：%s", sid, e)
                    d["cuts"] = []
            sigs[sid] = d
        return sigs[sid]

    prev_seg = None
    for seg, o0, o1 in output.timeline():
        i0, i1 = int(round(o0 / DT)), min(n, int(round(o1 / DT)))
        if i1 <= i0:
            continue
        sg = sig(seg.source)
        contiguous = (prev_seg is not None and prev_seg.source == seg.source and abs(prev_seg.end - seg.start) < 1e-3
                      and prev_seg.speed == seg.speed == 1.0 and not prev_seg.reverse and not seg.reverse)
        prev_seg = seg
        local = np.arange(i0, i1) * DT - o0
        content = seg.src_duration / seg.speed if sg["kind"] != "image" else 0.0
        live = local < content
        ts = (seg.end - local * seg.speed) if seg.reverse else (seg.start + local * seg.speed)
        if sg["kind"] == "image":
            has_v = True
            motion[i0:i1] = 0.01 if seg.evidence.get("motion", "kenburns") == "kenburns" else 0.0
            continue
        if sg["video"]:
            has_v = True
            idx = np.clip(np.round(ts / DT).astype(int), 0, len(sg["motion"]) - 1)
            motion[i0:i1] = np.where(live, sg["motion"][idx], 0.0)
            for c in sg["cuts"]:
                if seg.start <= c < seg.end and not seg.reverse:
                    cuts.append(o0 + (c - seg.start) / seg.speed)
            if o0 > 0 and not contiguous:
                cuts.append(o0)  # 方案里两个不相连的片段之间一定是画面跳变
        if sg["audio"] and not seg.mute:
            has_a = True
            idx = np.clip(np.round(ts / DT).astype(int), 0, len(sg["db"]) - 1)
            db[i0:i1] = np.where(live, sg["db"][idx], -100.0)
            voice[i0:i1] = live & sg["voice"][idx]
    for b in output.beds:
        if plan.sources[b.source].kind == "image":
            continue
        sg = sig(b.source)
        if not sg["audio"] or b.at >= total:
            continue
        has_a = True
        end = b.end if b.end is not None else (plan.sources[b.source].duration or b.start)
        i0, i1 = int(round(b.at / DT)), min(n, int(round((b.at + end - b.start) / DT)))
        if i1 <= i0:
            continue
        idx = np.clip(np.round((np.arange(i0, i1) * DT - b.at + b.start) / DT).astype(int), 0, len(sg["db"]) - 1)
        bed_db = sg["db"][idx] + b.gain_db
        db[i0:i1] = 10 * np.log10(10 ** (db[i0:i1] / 10) + 10 ** (bed_db / 10))
        if b.role == "narration":
            voice[i0:i1] |= sg["voice"][idx]
    size = output_size(plan, output)
    tl = _Tl(total, size, db, voice, motion if has_v else np.full(n, np.nan), sorted(set(cuts)), has_v, has_a)
    tl.will_normalize = size[1] > size[0]  # render 对竖屏输出默认 loudnorm
    # 响度估算：把主轨音频拼起来算整合响度
    try:
        tl.lufs = _plan_lufs(plan, output, sigs)
    except Exception as e:  # noqa: BLE001
        log.warning("响度估算失败：%s", e)
    caps = output.captions
    tl.captions = {"subtitle_streams": 0, "sidecar": [], "burned": bool(caps.enabled and caps.burn), "known_absent": not caps.enabled,
                   "sidecar_planned": bool(caps.enabled and not caps.burn)}
    # 文字稿与句子完整性
    toks = _tokens(plan, output, asr)
    tl.words = None
    if toks:
        words = [{"text": t.text, "start": t.start, "end": t.end, "punct": t.punct} for t in toks]
        edge = _plan_edge(plan, toks, lambda p_, sid: load_words(p_, sid, asr))
        _attach_words(tl, words, edge)
    else:
        tl.words_note = "没有文字稿（没有逐词文字稿，也没有可用的语音识别模型）"
    log.info("体检信号（方案 %s）：%.1fs，%.1fs", output.id, total, time.perf_counter() - t0)
    return tl


def _plan_lufs(plan: Plan, output, sigs: dict[str, dict[str, Any]]) -> float | None:
    out = np.zeros(int(output.duration * SR) + SR)
    for seg, o0, o1 in output.timeline():
        sg = sigs.get(seg.source)
        if not sg or "x" not in sg or seg.mute or sg["kind"] == "image":
            continue
        n = int((o1 - o0) * SR)
        pos = (seg.start + np.arange(n) / SR * seg.speed) * SR
        if seg.reverse:
            pos = seg.end * SR - np.arange(n) / SR * seg.speed * SR
        ok = (pos >= 0) & (pos < len(sg["x"])) & (np.arange(n) / SR < seg.src_duration / seg.speed)
        a = int(o0 * SR)
        seg_x = np.zeros(n)
        seg_x[ok] = sg["x"][pos[ok].astype(int)]
        out[a : a + n] += seg_x[: len(out) - a]
    for b in output.beds:
        sg = sigs.get(b.source)
        if not sg or "x" not in sg:
            continue
        end = b.end if b.end is not None else len(sg["x"]) / SR
        x = sg["x"][int(b.start * SR) : int(end * SR)] * 10 ** (b.gain_db / 20)
        a = int(b.at * SR)
        m = min(len(x), len(out) - a)
        if m > 0:
            out[a : a + m] += x[:m]
    return _lufs_np(out[: int(output.duration * SR)], SR)


def _plan_edge(plan: Plan, toks, load_words) -> dict[str, Any]:
    """方案里第一个/最后一个词在素材里的前后文：前一个词是不是句末，后一个词是不是紧跟着。"""
    sent_end = "。！？!?；;…"
    real = [t for t in toks if not t.punct]
    first, last = real[0], real[-1]
    res: dict[str, Any] = {"first_text": first.text, "last_text": last.text, "first_at": first.start, "last_at": last.end,
                           "start_mid": None, "end_mid": None}
    w1 = load_words(plan, first.src) or []
    prev = None
    for w in w1:
        if abs(float(w["start"]) - first.t0) < 1e-6 and w["text"] == first.text:
            break
        prev = w
    else:
        prev = None
    if prev is not None:
        gap = first.t0 - float(prev["end"])
        is_end = bool(prev["text"].strip()) and prev["text"].strip()[-1] in sent_end
        res["start_mid"] = (not is_end) and gap < 0.5
        res["start_ctx"] = "".join(str(w["text"]) for w in w1[max(0, w1.index(prev) - 6) : w1.index(prev) + 1])
    else:
        res["start_mid"] = False
    wl = load_words(plan, last.src) or []
    after = None
    seen = False
    for w in wl:
        if seen and not _is_p(w["text"]):
            after = w
            break
        if abs(float(w["start"]) - last.t0) < 1e-6 and w["text"] == last.text:
            seen = True
            # 紧跟的标点是句末就算完整
            j = wl.index(w) + 1
            if j < len(wl) and _is_p(wl[j]["text"]) and wl[j]["text"].strip()[-1:] in sent_end:
                res["end_mid"] = False
                return res
    if after is not None:
        gap = float(after["start"]) - last.t1
        res["end_mid"] = gap < 0.4
        res["end_ctx"] = str(after["text"])
    else:
        res["end_mid"] = False
    return res


def _is_p(text: str) -> bool:
    return bool(text.strip()) and all(c in "，。！？、；：,.!?;:…“”\"'（）()" for c in text.strip())


# ------------------------------------------------------------------ 检查


def _runs(mask: np.ndarray, min_len: float) -> list[tuple[float, float]]:
    """mask 为真的连续区间 [秒, 秒)，长度 ≥ min_len。"""
    m = np.concatenate([[False], mask, [False]])
    edges = np.flatnonzero(m[1:] != m[:-1])
    out = []
    for a, b in zip(edges[0::2], edges[1::2]):
        if (b - a) * DT >= min_len - 1e-9:
            out.append((a * DT, b * DT))
    return out


def _guess_platform(size: tuple[int, int]) -> str:
    w, h = size
    if not w or not h:
        return "youtube"
    return "douyin" if h / w >= 1.2 else "bilibili"


def check(target: Path | str | Plan, *, platform: str = "auto", output_id: str | None = None, asr: bool = True) -> CheckReport:
    """对成片或方案做发布前体检。platform：auto（按画幅猜）| douyin | tiktok | shorts | reels | bilibili | youtube。

    asr：True 时没有逐词文字稿就用本机语音识别补（慢，结果有缓存）；False 只用已有的文字稿，没有就只做声学检查。"""
    if platform != "auto" and platform not in PLATFORMS:
        raise ValueError(f"不认识的平台 {platform!r}；可选：auto、{', '.join(PLATFORMS)}")
    if isinstance(target, (str, Path)) and str(target).endswith(".json"):
        target = Plan.load(target)
    if isinstance(target, Plan):
        output = target.output(output_id)
        tl = _plan_tl(target, output, asr)
        name = f"{target.path or 'plan'}#{output.id}"
        mode = "plan"
    else:
        path = Path(target)
        if not path.exists():
            raise FileNotFoundError(f"找不到要体检的文件：{path}")
        tl = _file_tl(path, asr)
        name, mode = str(path), "file"
    guessed = platform == "auto"
    plat = _guess_platform(tl.size) if guessed else platform
    P = PLATFORMS[plat]
    rep = CheckReport(name, plat, tl.duration, mode=mode)
    items = rep.items
    m = rep.metrics
    n = len(tl.db)
    m["platform_guessed"] = guessed
    m["size"] = f"{tl.size[0]}x{tl.size[1]}" if tl.has_video else None
    voice_share = float(tl.voice.mean()) if n else 0.0
    m["voice_share"] = round(voice_share, 2)

    # ---- 静音 / 死寂 ----
    floor = float(np.percentile(tl.db, 10)) if tl.has_audio else -100.0
    silent = (tl.db < DIGITAL_SILENCE_DB) | (~tl.voice & (tl.db < min(floor + SILENCE_FLOOR_MARGIN, 99)) & (tl.db < SILENCE_ABS_DB))
    static = (np.nan_to_num(tl.motion, nan=0.0) < STATIC_MOTION) if tl.has_video else np.ones(n, dtype=bool)
    dead = _runs(silent[:n], DEAD_AIR_MIN)
    dead_list = []
    for a, b in dead:
        b = min(b, tl.duration)
        if b - a < DEAD_AIR_MIN - 1e-6:
            continue
        ia, ib = int(a / DT), int(b / DT)
        st = float(static[ia:ib].mean()) if ib > ia else 1.0
        dead_list.append({"at": round(a, 2), "end": round(b, 2), "static": round(st, 2)})
        dur = b - a
        if st >= 0.8:
            items.append(CheckItem(
                "dead_air", "risk", f"有 {dur:.1f}s 没有人声、声音很小、画面也几乎不动，容易被当成视频结束了（S88）",
                f"剪掉 {_mmss(a)}–{_mmss(b)}（保留约 0.4s 的呼吸），或在这里加一句话、一个画面变化/字幕强调", a, b))
        else:
            items.append(CheckItem(
                "dead_air", "warn", f"有 {dur:.1f}s 没有人声、声音很小（画面还在动）：观众只听到安静，可能怀疑设备出了问题",
                f"把 {_mmss(a)}–{_mmss(b)} 缩到 0.4–0.6s；确实要留白（操作演示）就加字幕或音效说明", a, b))
    m["dead_air"] = dead_list
    m["dead_air_total_s"] = round(sum(d["end"] - d["at"] for d in dead_list), 2)

    # ---- 开头 ----
    first_voice = float(np.argmax(tl.voice) * DT) if tl.voice.any() else None
    audible = ~silent[:n]
    first_sound = float(np.argmax(audible) * DT) if audible.any() else None
    first_cut = next((c for c in tl.cuts if c > 0.3), None)
    m["first_voice_s"] = None if first_voice is None else round(first_voice, 2)
    m["first_sound_s"] = None if first_sound is None else round(first_sound, 2)
    m["first_cut_s"] = None if first_cut is None else round(first_cut, 2)
    head = int(HOOK_WARN / DT)
    head_dead = bool(silent[:head].all()) and bool(static[:head].all()) if n >= head else False
    m["opening_empty"] = head_dead
    if head_dead:
        items.append(CheckItem("hook_empty", "risk", f"前 {HOOK_WARN:.0f}s 没有声音也没有画面变化，观众还没看到内容就可能划走（F2）",
                               f"从第一个有内容的地方开始（声音出现在 {_mmss(first_sound) if first_sound is not None else '—'}），剪掉开头的空白；"
                               "或在第 0 秒加一句点题的话 / 一个有冲击力的画面", 0.0, HOOK_WARN))
    elif voice_share >= 0.2 and first_voice is not None and first_voice > HOOK_WARN and not (first_cut is not None and first_cut <= HOOK_WARN):
        sev = "risk" if first_voice > HOOK_RISK else "warn"
        first_line = next((w["text"] for w in (tl.words or []) if not w.get("punct")), "")
        items.append(CheckItem(
            "hook_late", sev, f"开头 {first_voice:.1f}s 才出现第一句话（也没有明显的画面变化）；前 3 秒是观众决定留不留的时候（经验值，非平台规则）",
            f"把 {_mmss(first_voice)} 的那句点题的话挪到开头，或剪掉前 {max(first_voice - 0.3, 0):.1f}s" + (f"（第一句是「{first_line[:12]}…」）" if first_line else ""),
            0.0, first_voice))

    # ---- 节奏 ----
    bounds = [0.0] + [c for c in tl.cuts if 0 < c < tl.duration] + [tl.duration]
    shots = np.diff(bounds)
    if tl.has_video and len(shots):
        longest = int(np.argmax(shots))
        m["shots"] = {"count": int(len(shots)), "median_s": round(float(np.median(shots)), 2), "max_s": round(float(shots.max()), 2),
                      "max_at": round(bounds[longest], 2)}
        if tl.duration > 20 and float(np.median(shots)) > 15 and not dead_list:
            items.append(CheckItem(
                "pacing", "info", f"镜头/画面平均每 {float(np.median(shots)):.0f}s 才变一次（最长 {shots.max():.0f}s）；长时间同一画面容易让人划走",
                "每 5–10 秒加一次画面变化：切近景、放大、插图、字幕强调（这是经验值，访谈/课程类可以更慢）",
                bounds[longest], bounds[longest + 1]))
        stat_runs = _runs(static[:n], STATIC_LONG)
        longest_static = max(((b - a, a, b) for a, b in stat_runs), default=(0.0, 0.0, 0.0))
        m["longest_static_s"] = round(longest_static[0], 2)
        for a, b in stat_runs:
            items.append(CheckItem("static_long", "warn", f"画面 {min(b, tl.duration) - a:.0f}s 几乎没有变化",
                                   "在这段里加一次画面变化：放大、插入图片/录屏、切镜头，或加字幕强调重点", a, min(b, tl.duration)))

    # ---- 字幕 ----
    c = tl.captions
    if c.get("burned"):
        m["captions"] = "burned"
    elif c.get("sidecar") or c.get("subtitle_streams") or c.get("sidecar_planned"):
        m["captions"] = "sidecar"
        items.append(CheckItem("captions_sidecar", "info", "字幕是单独的文件/软字幕，没有烧进画面；上传到短视频平台时通常不会带上",
                               "短视频平台出片时烧录字幕（render 的 burn_captions=True），横屏长视频可以保留 srt 另传"))
    elif c.get("known_absent"):
        m["captions"] = "none"
        sev = "warn" if voice_share >= 0.2 and P.orient == "vertical" else "info"
        items.append(CheckItem("captions_missing", sev, "方案没有开启字幕；静音刷视频的观众看不懂人在说什么",
                               "在输出里把 captions.enabled 和 burn 设为 true 再出片" if voice_share >= 0.2 else "没有人声的视频可忽略"))
    else:
        m["captions"] = "unknown"
        items.append(CheckItem("captions_unknown", "info", "无法确认是否有烧录字幕：没有字幕流，也没有同名 .srt（画面里是否有字幕需要人看一眼）",
                               "说话为主的视频，用 autocut 出片时开启 captions.burn；已经烧录了可以忽略这一条"))

    # ---- 响度 ----
    if tl.has_audio and tl.lufs is not None:
        m["loudness"] = {"integrated_lufs": round(tl.lufs, 1), "true_peak_dbtp": None if tl.true_peak is None else round(tl.true_peak, 1),
                         "lra": None if tl.lra is None else round(tl.lra, 1), "target_lufs": P.loudness, "estimated": mode == "plan"}
        d = tl.lufs - P.loudness
        if mode == "plan" and tl.will_normalize:
            m["loudness"]["note"] = "渲染时会标准化到目标响度"
        elif d < -LOUDNESS_TOL:
            sev = "risk" if d < -8 else "warn"
            items.append(CheckItem("loudness_low", sev, f"整合响度 {tl.lufs:.1f} LUFS，比 {P.label} 常用的 {P.loudness:.0f} LUFS 低 {-d:.1f} dB，手机外放会偏小声",
                                   f"出片时开启响度标准化（loudnorm 到 {P.loudness:.0f} LUFS）；竖屏输出默认已开启"))
        elif d > LOUDNESS_TOL:
            items.append(CheckItem("loudness_high", "warn", f"整合响度 {tl.lufs:.1f} LUFS，比 {P.label} 常用的 {P.loudness:.0f} LUFS 高 {d:.1f} dB，平台会压低音量，还可能失真",
                                   f"出片时 loudnorm 到 {P.loudness:.0f} LUFS，或把总增益降 {d:.0f} dB"))
        if tl.true_peak is not None and tl.true_peak > TRUE_PEAK_MAX:
            items.append(CheckItem("true_peak", "risk" if tl.true_peak > 0 else "warn", f"真峰值 {tl.true_peak:.1f} dBTP，超过 {TRUE_PEAK_MAX:.0f} dBTP，平台转码后可能削波爆音",
                                   "出片时加限幅器（render 的 loudnorm 带 -1.5 dBTP 限幅），或把增益降低"))
    elif tl.has_audio:
        m["loudness"] = {"integrated_lufs": None, "note": "整段接近静音"}
    else:
        items.append(CheckItem("no_audio", "risk", "没有音频流", "补一条人声或 BGM；无声视频在多数平台的推荐里会被压制"))

    # ---- 画幅、分辨率、时长 ----
    w, h = tl.size
    if tl.has_video and w and h:
        vertical = h / w >= 1.2
        if P.orient == "vertical" and not vertical:
            items.append(CheckItem("aspect", "risk", f"画幅 {w}x{h}（{w / h:.2f}:1 横屏），{P.label}是竖屏平台（9:16）：横屏会缩成屏幕中间一条，或被裁掉两侧",
                                   "输出改成 1080x1920 并给输出加 reframe（mode=center 居中裁切，主体不在中间就用 keyframes 跟随）"))
        elif P.orient == "horizontal" and vertical:
            items.append(CheckItem("aspect", "warn" if plat == "youtube" else "info", f"画幅 {w}x{h} 是竖屏，{P.label}以 16:9 横屏为主，电脑端两侧会留黑",
                                   "如果主要发布在这个平台，输出改成 1920x1080 并 reframe；竖屏内容可以保留，但先确认主要观看设备"))
        elif P.orient == "vertical" and abs(h / w - 16 / 9) > 0.25:
            items.append(CheckItem("aspect", "warn", f"画幅 {w}x{h} 不是标准 9:16（{h / w:.2f}:1），上下会有黑边或被裁",
                                   "输出改成 1080x1920"))
        short_side = min(w, h)
        m["short_side_px"] = short_side
        if short_side < 720:
            items.append(CheckItem("resolution", "warn", f"分辨率 {w}x{h} 偏低（短边 {short_side}px），平台会放大显示，画面发糊",
                                   "出片分辨率至少 720p，竖屏推荐 1080x1920；素材本身就低的话，源素材重新导出"))
    m["duration_s"] = round(tl.duration, 2)
    if tl.duration < MIN_CUE_SHORT:
        items.append(CheckItem("duration", "warn", f"成片只有 {tl.duration:.1f}s，太短，观众还没反应过来就结束了", "补足内容，或合并相邻的几条"))
    if P.hard_max and tl.duration > P.hard_max:
        items.append(CheckItem("duration", "risk", f"时长 {tl.duration:.0f}s 超出 {P.label}的常见上限 {P.hard_max:.0f}s（{P.note}）",
                               f"剪到 {P.hard_max:.0f}s 内，或按章节/高光切成多条，每条 {P.soft_max or 60:.0f}s 左右"))
    elif P.soft_max and tl.duration > P.soft_max:
        items.append(CheckItem("duration", "warn", f"时长 {tl.duration:.0f}s 超过 {P.label}短视频的常见时长 {P.soft_max:.0f}s（{P.note}）",
                               f"砍掉铺垫和重复，剪到 {P.soft_max:.0f}s 内；信息量大就按话题切成多条"))

    # ---- 开头和结尾落在句子中间 ----
    if tl.has_audio and n > 4 and tl.voice.any():
        voice_med = float(np.median(tl.db[tl.voice]))  # 说话时的典型电平；开头/结尾的电平离它不远，说明话没说完
        if tl.voice[:2].all() and tl.db[0] > voice_med - 12.0:
            items.append(CheckItem("start_midspeech", "warn", "第一帧就已经在说话，开头像是切在了词的中间", "开头往前补 0.2–0.3s，或确认这确实是句子的开头",
                                   0.0, 0.2))
        end_i = int(tl.duration / DT)
        if tl.voice[max(end_i - 3, 0) : end_i].all() and tl.db[min(end_i - 1, n - 1)] > voice_med - 12.0:
            items.append(CheckItem("end_midspeech", "warn", "结尾处还在说话，最后像是被硬切断的", "结尾往后延 0.3–0.5s 到这句话说完，或在句末停顿处收尾",
                                   max(tl.duration - 0.3, 0), tl.duration))
    e = tl.edge
    if e:
        if e.get("start_mid"):
            items.append(CheckItem("start_midsentence", "warn", f"开头落在一句话中间：第一个词「{e['first_text']}」前面还有半句（{e.get('start_ctx', '')[-12:]}）",
                                   "把片段起点往前挪到这句话的开头（上一个句号之后），否则观众会听到没头没尾的半句", e.get("first_at", 0.0), (e.get("first_at", 0.0) + 1.0)))
        if e.get("end_mid"):
            items.append(CheckItem("end_midsentence", "warn", f"结尾落在一句话中间：最后一个词「{e['last_text']}」后面还有话没说完" + (f"（接着是「{e['end_ctx']}」）" if e.get("end_ctx") else ""),
                                   "把片段终点往后挪到这句话说完（句号处），或砍到上一句结束", max(e.get("last_at", tl.duration) - 1.0, 0.0), e.get("last_at", tl.duration)))
    if tl.words is None and tl.has_audio and voice_share >= 0.2:
        items.append(CheckItem("no_words", "info", f"{tl.words_note}：只做了声学检查，无法判断开头/结尾是否落在句子中间，也无法核对字幕",
                               "先生成逐词文字稿（AUTOCUT_WHISPER_MODEL 指向 whisper 模型）再体检"))
    for it in items:  # 全片性的问题（响度、画幅、时长、字幕）没有具体时间点：标成整段，方便存档后和留存曲线对齐
        if it.at is None:
            it.at, it.end = 0.0, tl.duration
    log.info("体检 %s：%d 项（risk %d / warn %d）", name, len(items), sum(i.severity == "risk" for i in items), sum(i.severity == "warn" for i in items))
    return rep

