"""创意配方共用的小工具：素材登记、画幅、节拍网格、滚动中位数。

只放四个配方（highlights / montage / ytpmv / narrate）真正共用的东西；各自的判断逻辑留在各自文件里。
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from autocut.analysis import Media
from autocut.beats import BeatResult
from autocut.plan import Plan, Source

log = logging.getLogger(__name__)

# 拍号假设与 beats.py 一致：4/4
BEATS_PER_BAR = 4
# 检测出的 BPM 离整数不超过这么多，就认为曲子本来就是整数 BPM（流行音乐绝大多数如此），用整数
_BPM_SNAP = 0.25
# 第一个小节首拍允许比 0 早这么多秒（onset 检测对起音有几毫秒的系统偏差），早于此视为曲子从第 0 秒开始
_START_SLACK = 0.03


class RecipeError(ValueError):
    """配方无法继续：消息说明发生了什么、哪个文件或选项、怎么修。"""


# ------------------------------------------------------------------ 素材与路径


def default_plan_path(first: Path) -> Path:
    """默认方案路径：第一个素材旁边的 <stem>.autocut.json。"""
    return first.parent / f"{first.stem}.autocut.json"


def plan_path_for(first: Path, out: Path | None) -> Path:
    return Path(out).resolve() if out else default_plan_path(first.resolve())


def stored_path(p: Path, plan_path: Path) -> str:
    """素材在方案里的路径：在方案目录之内就存相对路径（整个目录搬走方案仍然有效），否则存绝对路径。"""
    p = p.resolve()
    try:
        return os.path.relpath(p, plan_path.parent) if p.is_relative_to(plan_path.parent) else str(p)
    except ValueError:
        return str(p)


def unique_id(hint: str, taken: set[str] | dict) -> str:
    """素材 id：取文件名主干，去掉空白；重名加 _2、_3。"""
    base = re.sub(r"\s+", "_", hint).strip("_") or "src"
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}_{n}", n + 1
    return sid


def make_source(sid: str, media: Media, plan_path: Path, kind: str | None = None) -> Source:
    """用探测结果填一个 Source（时长、尺寸、帧率）。"""
    info = media.info
    v = info.video
    k = kind or ("video" if v else "audio")
    return Source(
        id=sid,
        path=stored_path(media.path, plan_path),
        kind=k,
        duration=round(info.duration, 4),
        width=v.width if v else None,
        height=v.height if v else None,
        fps=round(v.fps, 3) if v else None,
    )


def plain(x: Any) -> Any:
    """numpy 标量 / 数组 / 元组 → 原生 Python 类型。方案要能 json 序列化，而且 np.float64 == [] 这类比较会让 plan._prune 报错。"""
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, dict):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        for f in dataclasses.fields(x):
            setattr(x, f.name, plain(getattr(x, f.name)))
        return x
    return x


def finalize(plan: Plan) -> Plan:
    """配方出口：把方案里所有 numpy 类型换成原生类型，再校验。"""
    for src in plan.sources.values():
        plain(src)
    for o in plan.outputs:
        plain(o)
    plan.options = plain(plan.options)
    plan.validate()
    return plan


# ------------------------------------------------------------------ 画幅

_ASPECT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:/x]\s*(\d+(?:\.\d+)?)\s*$")


def aspect_size(aspect: str | None, short: int = 1080) -> tuple[int, int] | None:
    """"9:16" → (1080, 1920)；短边取 short，宽高取偶数。None / "source" → None（跟随素材）。"""
    if aspect is None or str(aspect).lower() in ("", "source", "none"):
        return None
    m = _ASPECT_RE.match(str(aspect))
    if not m:
        raise RecipeError(f"aspect={aspect!r} 看不懂：请写成 '宽:高'，例如 '9:16'、'16:9'、'1:1'、'4:5'，或 None 跟随素材")
    w, h = float(m.group(1)), float(m.group(2))
    if w <= 0 or h <= 0:
        raise RecipeError(f"aspect={aspect!r} 的宽高必须为正")
    if w <= h:
        sw, sh = short, short * h / w
    else:
        sh, sw = short, short * w / h
    return int(round(sw / 2) * 2), int(round(sh / 2) * 2)


def mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


# ------------------------------------------------------------------ 数值

def rolling_percentile(x: np.ndarray, half: int, q: float = 50.0) -> np.ndarray:
    """中心滚动分位数（q ∈ [0, 100]），窗口 2*half+1，两端用反射补齐。q=50 就是滚动中位数。"""
    n = len(x)
    if n == 0 or half <= 0:
        return x.astype(np.float64).copy()
    half = min(half, max(n - 1, 1))
    xp = np.pad(x.astype(np.float64), (half, half), mode="reflect")
    win = np.lib.stride_tricks.sliding_window_view(xp, 2 * half + 1)
    return np.percentile(win, q, axis=1)


def rolling_median(x: np.ndarray, half: int) -> np.ndarray:
    return rolling_percentile(x, half, 50.0)


def runs(mask: np.ndarray, merge_gap: int = 0) -> list[tuple[int, int]]:
    """布尔数组里连续 True 的 [起, 止] 下标（含止）；间隔不超过 merge_gap 个 False 的段合并。"""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    out: list[list[int]] = [[int(idx[0]), int(idx[0])]]
    for i in idx[1:]:
        if i - out[-1][1] - 1 <= merge_gap:
            out[-1][1] = int(i)
        else:
            out.append([int(i), int(i)])
    return [(a, b) for a, b in out]


def noisy_or(parts: list[float]) -> float:
    """多个独立证据的合成：1 - Π(1 - p)。每个 p ∈ [0, 1]；多条信号同时成立得分更高，但不会超过 1。"""
    left = 1.0
    for p in parts:
        left *= 1.0 - min(max(p, 0.0), 1.0)
    return 1.0 - left


# ------------------------------------------------------------------ 节拍网格


@dataclass(frozen=True)
class BeatGrid:
    """把检测出的、带抖动的拍点拟合成一条等距网格：第 k 拍的时刻 = offset + k·period。

    卡点要的是"精确到拍"：每个镜头的 slot 取 period 的整数倍，累加起来不会漂移；
    检测值直接用的话，每拍有几毫秒抖动，剪点会"差一点点"。
    """

    bpm: float
    period: float  # 秒/拍
    offset: float  # 第 0 拍的时刻
    bar_k0: int  # 第一个时刻 ≥ 0 的小节首拍的拍序号
    duration: float  # 音乐总时长
    confidence: float
    snapped: bool  # BPM 是否吸附到了整数

    @property
    def bar(self) -> float:
        return self.period * BEATS_PER_BAR

    def time(self, k: float) -> float:
        return self.offset + k * self.period

    def bar_start(self, j: int) -> float:
        """第 j 个完整小节（0 起）的首拍时刻。"""
        return self.time(self.bar_k0 + BEATS_PER_BAR * j)

    @property
    def n_bars(self) -> int:
        """从第一个小节首拍起，音乐里放得下几个完整小节。"""
        return max(0, int(np.floor((self.duration - self.bar_start(0) + 1e-6) / self.bar)))


def fit_beat_grid(res: BeatResult, duration: float, source_name: str = "音乐") -> BeatGrid:
    if len(res.beats) < 4 or res.bpm <= 0:
        raise RecipeError(
            f"{source_name} 里只检测到 {len(res.beats)} 个拍点（置信度 {res.confidence:.2f}），不足以卡点；"
            "请换一首节奏更清晰的曲子，或确认文件不是纯环境音"
        )
    b = np.asarray(res.beats, dtype=np.float64)
    t0 = float(np.median(np.diff(b)))
    k = np.round((b - b[0]) / t0)
    # 最小二乘拟合 b = a + k·T；剔除一次离群点（偶发的漏拍/多拍）
    keep = np.ones(len(b), dtype=bool)
    for _ in range(2):
        slope, a = np.polyfit(k[keep], b[keep], 1)
        resid = np.abs(b - (a + slope * k))
        keep = resid <= max(0.05 * slope, 3 * float(np.median(resid[keep])) + 1e-4)
        if keep.sum() < 4:
            keep[:] = True
            break
    period, offset = float(slope), float(a)
    bpm = 60.0 / period
    snapped = abs(bpm - round(bpm)) <= _BPM_SNAP and round(bpm) > 0
    if snapped:
        bpm = float(round(bpm))
        period = 60.0 / bpm
        offset = float(np.median(b - (k * period)))  # 周期定了，重估偏移
    if -_START_SLACK <= offset < 0:
        offset = 0.0
    # 小节首拍的相位：检测到的 downbeat 对应的拍序号模 4 的众数
    phase = 0
    if res.downbeats:
        ks = np.round((np.asarray(res.downbeats) - offset) / period).astype(int) % BEATS_PER_BAR
        phase = int(np.bincount(ks, minlength=BEATS_PER_BAR).argmax())
    k_min = int(np.ceil((0.0 - offset) / period - 1e-6))
    bar_k0 = k_min + ((phase - k_min) % BEATS_PER_BAR)
    grid = BeatGrid(bpm=bpm, period=period, offset=offset, bar_k0=bar_k0, duration=float(duration),
                    confidence=float(res.confidence), snapped=snapped)
    log.info("节拍网格：%.3f BPM%s，周期 %.5fs，偏移 %.4fs，首个小节首拍 %.4fs，置信度 %.2f，共 %d 个完整小节",
             bpm, "（吸附到整数）" if snapped else "", period, offset, grid.bar_start(0), res.confidence, grid.n_bars)
    return grid


def plan_bars(length: float, grid: BeatGrid) -> tuple[int, str]:
    """目标秒数 → 整数个小节（取最接近的；正好在两者中间取短的，短视频宁短勿长）。"""
    if length <= 0:
        raise RecipeError(f"length={length}：目标时长必须为正（秒）")
    lo = max(int(np.floor(length / grid.bar + 1e-9)), 1)
    hi = lo + 1
    pick = lo if abs(length - lo * grid.bar) <= abs(hi * grid.bar - length) + 1e-9 else hi
    return pick, f"目标 {length:g}s → {pick} 小节 = {pick * grid.bar:.2f}s（{grid.bpm:g} BPM，每小节 {grid.bar:.2f}s）"


def choose_music_start(music: Media, grid: BeatGrid, bars: int) -> tuple[int, str]:
    """从第几个小节开始用。曲子刚好够长就从头；更长就取响度最大的连续 bars 个小节（副歌 / 高潮），响度差 < 0.5dB 取靠前的。"""
    avail = grid.n_bars
    if avail <= bars:
        return 0, "曲子不比成片长多少，从头开始"
    x = music.audio(sr=22050)
    sr = 22050
    ms = []
    for j in range(avail):
        a = int(max(grid.bar_start(j), 0.0) * sr)
        b = int((grid.bar_start(j) + grid.bar) * sr)
        seg = x[a:b]
        ms.append(float(np.mean(seg.astype(np.float64) ** 2)) if len(seg) else 0.0)
    ms_arr = np.asarray(ms)
    sums = np.array([ms_arr[j : j + bars].mean() for j in range(avail - bars + 1)])
    db = 10 * np.log10(np.maximum(sums, 1e-12))
    j0 = int(np.flatnonzero(db >= db.max() - 0.5)[0])
    return j0, ("从头开始（开头就是最响的一段）" if j0 == 0 else f"从第 {j0 + 1} 小节开始（连续 {bars} 小节里最响，约 {db[j0]:.0f}dB）")
