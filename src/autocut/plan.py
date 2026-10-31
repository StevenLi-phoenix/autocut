"""方案（Plan）：autocut 所有配方的唯一产物，也是人、agent、渲染器共用的事实来源。

一份方案 = 若干素材（sources）+ 若干输出（outputs）。每个输出是一条时间线：
- segments：主轨，按列表顺序拼接 action=keep 的片段；action=cut 的片段留在列表里，
  这样审阅时可以一键改回保留（"修订模式"）。
- beds：铺在输出时间轴上的音频（BGM、配音），可以对主轨做闪避（ducking）。
- overlays：叠在画面上的图片/文字。
- captions：字幕从哪来、是否烧录。

时间单位一律是秒（float）。segment 的 start/end 是**素材时间**；bed/overlay 的 at 是**输出时间**。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 剪/留的理由。值是给人看的中文标签。
REASONS: dict[str, str] = {
    "speech": "说话",
    "pause": "自然停顿",
    "silence": "长停顿",
    "retake": "重说",
    "filler": "口头禅",
    "action": "无解说但有看点",
    "idle": "无解说也无看点",
    "highlight": "高光",
    "lead_in": "铺垫",
    "beat": "卡点",
    "syllable": "音节",
    "shot": "镜头",
    "image": "配图",
    "manual": "手动",
}
ACTIONS = ("keep", "cut")
SOURCE_KINDS = ("video", "audio", "image")
# 把握度低于这个值的决定会进入"拿不准"队列
UNCERTAIN_BELOW = 0.75


class PlanError(ValueError):
    """方案不合法。消息指出具体字段路径、错误值和合法取值，方便人和 agent 直接改。"""


@dataclass
class Source:
    id: str
    path: str  # 相对方案文件所在目录的路径，或绝对路径
    kind: str = "video"
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_track: int | None = None  # 输出用哪条音轨（音轨序号，0 起）；None = 第一条
    analysis_track: int | None = None  # 判断用哪条音轨（例如 OBS 的 mic 轨）
    words: str | None = None  # 逐词文字稿缓存文件（JSON）的路径，供字幕和审阅页使用
    warnings: list[str] = field(default_factory=list)


@dataclass
class Segment:
    id: str
    source: str
    start: float
    end: float
    action: str = "keep"
    reason: str = "speech"
    confidence: float = 1.0
    detail: str = ""  # 给人看的一句话理由，例如"停顿 2.6s → 保留 0.4s"
    text: str = ""  # 这段里说了什么（文字稿），审阅页显示用
    locked: bool = False  # 锁定后配方重跑也不改它
    slot: float | None = None  # 指定输出时长：素材不够长就定格补齐，太长就截断（卡点/鬼畜用）
    speed: float = 1.0
    pitch: float = 0.0  # 半音
    reverse: bool = False
    mute: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)  # 判断依据的数值，例如 {"gap": 2.6}

    @property
    def src_duration(self) -> float:
        return self.end - self.start

    @property
    def out_duration(self) -> float:
        if self.slot is not None:
            return self.slot
        return (self.end - self.start) / self.speed

    @property
    def uncertain(self) -> bool:
        return self.confidence < UNCERTAIN_BELOW


@dataclass
class Bed:
    """铺在输出时间轴上的一段音频（BGM、配音）。"""

    source: str
    at: float = 0.0  # 输出时间
    start: float = 0.0  # 素材时间
    end: float | None = None  # None = 到素材结尾
    gain_db: float = 0.0
    duck_db: float = 0.0  # >0：主轨有声音时把这条压低多少 dB（BGM 让位于人声）
    duck_main_db: float = 0.0  # >0：这条有声音时把主轨压低多少 dB（游戏原声让位于解说）
    role: str = "music"  # music | narration


@dataclass
class Overlay:
    kind: str  # image | text
    at: float
    duration: float
    source: str | None = None  # kind=image 时引用 sources
    text: str = ""
    position: str = "center"  # center | top | bottom | full
    motion: str = "none"  # none | kenburns


@dataclass
class Captions:
    enabled: bool = False
    burn: bool = False  # True = 烧录进画面；False = 只出 .srt 侧车文件
    source: str | None = None  # 从哪个素材的文字稿生成；None = 主轨素材
    max_chars: int = 16  # 每行最多字符数（中文）；英文按 2 倍计


@dataclass
class Reframe:
    """改变画幅时的裁切框。keyframes: [[输出时间, 中心x(0–1), 中心y(0–1)], ...]，中间线性插值。"""

    mode: str = "center"  # center | track
    keyframes: list[list[float]] = field(default_factory=list)


@dataclass
class Output:
    id: str
    segments: list[Segment] = field(default_factory=list)
    width: int | None = None  # None = 跟随主素材
    height: int | None = None
    fps: float | None = None
    beds: list[Bed] = field(default_factory=list)
    overlays: list[Overlay] = field(default_factory=list)
    captions: Captions = field(default_factory=Captions)
    reframe: Reframe | None = None
    audio_fade_ms: float = 12.0  # 每个剪口两侧的淡入淡出，消除爆音
    selected: bool = True  # 候选清单里没选中的输出不出片
    title: str = ""
    score: float | None = None
    reasons: list[str] = field(default_factory=list)

    def kept(self) -> list[Segment]:
        return [s for s in self.segments if s.action == "keep"]

    @property
    def duration(self) -> float:
        return sum(s.out_duration for s in self.kept())

    def timeline(self) -> list[tuple[Segment, float, float]]:
        """保留片段在输出时间轴上的位置：[(segment, 输出起点, 输出终点), ...]。"""
        out, t = [], 0.0
        for s in self.kept():
            out.append((s, t, t + s.out_duration))
            t += s.out_duration
        return out

    def to_output_time(self, source: str, t: float) -> float | None:
        """素材时间 → 输出时间；落在被剪掉的部分时返回 None。只对未变速、未定格的片段精确。"""
        for s, o0, _ in self.timeline():
            if s.source == source and s.start <= t <= s.end:
                return o0 + (t - s.start) / s.speed
        return None

    def find(self, seg_id: str) -> tuple[int, Segment]:
        for i, s in enumerate(self.segments):
            if s.id == seg_id:
                return i, s
        raise PlanError(f"输出 {self.id!r} 里没有 id 为 {seg_id!r} 的片段；可用的 id 例如 {[s.id for s in self.segments[:5]]}")


@dataclass
class Plan:
    recipe: str
    sources: dict[str, Source]
    outputs: list[Output]
    options: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # 给人和 agent 的提示，例如"没有语音识别模型，只剪了停顿"
    version: int = SCHEMA_VERSION
    revision: int = 0  # 每次保存 +1；审阅页用它发现外部修改
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    path: Path | None = field(default=None, repr=False, compare=False)

    # ---- 查询 ----
    def output(self, output_id: str | None = None) -> Output:
        if output_id is None:
            return self.outputs[0]
        for o in self.outputs:
            if o.id == output_id:
                return o
        raise PlanError(f"没有 id 为 {output_id!r} 的输出；可用：{[o.id for o in self.outputs]}")

    def source_path(self, source_id: str) -> Path:
        """解析素材的绝对路径（相对路径以方案文件所在目录为基准）。"""
        p = Path(self.sources[source_id].path)
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent / p
        return p

    def summary(self) -> dict[str, Any]:
        """方案摘要：人和 agent 都先看这个，而不是整份方案。"""
        outs = []
        for o in self.outputs:
            cut = [s for s in o.segments if s.action == "cut"]
            counts: dict[str, int] = {}
            for s in cut:
                counts[s.reason] = counts.get(s.reason, 0) + 1
            src_total = sum(s.src_duration for s in o.segments)
            item: dict[str, Any] = {
                "id": o.id,
                "duration": round(o.duration, 2),
                "segments": len(o.segments),
                "kept": len(o.kept()),
                "cut": counts,
                "uncertain": [s.id for s in o.segments if s.uncertain],
                "selected": o.selected,
            }
            if cut:
                item["source_span"] = round(src_total, 2)
                item["removed"] = round(sum(s.src_duration for s in cut), 2)
            if o.title:
                item["title"] = o.title
            if o.score is not None:
                item["score"] = o.score
            if o.reasons:
                item["reasons"] = o.reasons
            if o.width and o.height:
                item["size"] = f"{o.width}x{o.height}"
            outs.append(item)
        return {
            "recipe": self.recipe,
            "plan": str(self.path) if self.path else None,
            "sources": {k: {"path": v.path, "duration": v.duration} for k, v in self.sources.items()},
            "outputs": outs,
            "notes": self.notes,
        }

    # ---- 修改（审阅页和 agent 都走这些函数，保证不变量）----
    def set_action(self, seg_id: str, action: str, output_id: str | None = None) -> Segment:
        if action not in ACTIONS:
            raise PlanError(f"action 只能是 {ACTIONS}，收到 {action!r}")
        _, seg = self.output(output_id).find(seg_id)
        seg.action = action  # reason 保留机器当初的判断，方便事后对照
        seg.confidence = 1.0  # 人确认过的决定不再算"拿不准"
        seg.evidence["edited"] = True
        return seg

    def move_boundary(self, seg_id: str, edge: str, t: float, output_id: str | None = None) -> None:
        """移动片段的起点或终点；如果相邻片段与它首尾相接（同一素材连续），一起移动，保持时间轴无缝。"""
        if edge not in ("start", "end"):
            raise PlanError(f"edge 只能是 'start' 或 'end'，收到 {edge!r}")
        out = self.output(output_id)
        i, seg = out.find(seg_id)
        nb_i = i - 1 if edge == "start" else i + 1
        nb = out.segments[nb_i] if 0 <= nb_i < len(out.segments) else None
        old = getattr(seg, edge)
        joined = nb is not None and nb.source == seg.source and abs((nb.end if edge == "start" else nb.start) - old) < 1e-6
        if edge == "start":
            lo = nb.start + 0.01 if joined else 0.0
            hi = seg.end - 0.01
        else:
            lo = seg.start + 0.01
            hi = nb.end - 0.01 if joined else (self.sources[seg.source].duration or float("inf"))
        if not lo <= t <= hi:
            raise PlanError(f"{seg_id}.{edge}={t:.3f} 超出允许范围 [{lo:.3f}, {hi:.3f}]")
        setattr(seg, edge, round(t, 4))
        if joined:
            setattr(nb, "end" if edge == "start" else "start", round(t, 4))
        seg.evidence["edited"] = True

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("path", None)
        d = {"autocut_plan": SCHEMA_VERSION, **d}
        return _prune(d)

    def save(self, path: str | Path | None = None) -> Path:
        """原子写入（临时文件 + rename），revision +1。"""
        target = Path(path) if path else self.path
        if target is None:
            raise PlanError("保存方案需要路径")
        self.path = target.resolve()
        self.revision += 1
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=1)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".plan-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(data + "\n")
        os.replace(tmp, self.path)
        log.info("方案已保存：%s（revision %d）", self.path, self.revision)
        return self.path

    @classmethod
    def from_dict(cls, d: dict[str, Any], path: Path | None = None) -> Plan:
        if not isinstance(d, dict) or "outputs" not in d:
            raise PlanError("这不是 autocut 方案文件：缺少 outputs 字段")
        ver = d.get("autocut_plan", d.get("version"))
        if ver != SCHEMA_VERSION:
            raise PlanError(f"方案版本 {ver} 与本程序支持的版本 {SCHEMA_VERSION} 不一致")
        try:
            sources = {k: _build(Source, {"id": k, **v}) for k, v in d.get("sources", {}).items()}
            outputs = []
            for o in d["outputs"]:
                o = dict(o)
                o["segments"] = [_build(Segment, s) for s in o.get("segments", [])]
                o["beds"] = [_build(Bed, b) for b in o.get("beds", [])]
                o["overlays"] = [_build(Overlay, x) for x in o.get("overlays", [])]
                o["captions"] = _build(Captions, o.get("captions", {}))
                o["reframe"] = _build(Reframe, o["reframe"]) if o.get("reframe") else None
                outputs.append(_build(Output, o))
        except TypeError as e:
            raise PlanError(f"方案字段不合法：{e}") from e
        plan = cls(
            recipe=d.get("recipe", "manual"),
            sources=sources,
            outputs=outputs,
            options=d.get("options", {}),
            notes=d.get("notes", []),
            version=SCHEMA_VERSION,
            revision=int(d.get("revision", 0)),
            created=d.get("created", ""),
            path=path,
        )
        plan.validate()
        return plan

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        p = Path(path).resolve()
        try:
            d = json.loads(p.read_text())
        except FileNotFoundError:
            raise PlanError(f"找不到方案文件 {p}") from None
        except json.JSONDecodeError as e:
            raise PlanError(f"方案文件不是合法 JSON：{p}:{e.lineno}:{e.colno} {e.msg}") from None
        return cls.from_dict(d, path=p)

    def validate(self) -> None:
        """检查不变量。错误消息给出字段路径和合法取值。"""
        errs: list[str] = []
        if not self.outputs:
            errs.append("outputs 不能为空")
        for sid, s in self.sources.items():
            if s.kind not in SOURCE_KINDS:
                errs.append(f"sources.{sid}.kind={s.kind!r}，应为 {SOURCE_KINDS} 之一")
        ids_seen: set[str] = set()
        for oi, o in enumerate(self.outputs):
            if o.id in ids_seen:
                errs.append(f"outputs[{oi}].id={o.id!r} 重复")
            ids_seen.add(o.id)
            seg_ids: set[str] = set()
            for si, s in enumerate(o.segments):
                at = f"outputs[{oi}].segments[{si}]（id={s.id}）"
                if s.id in seg_ids:
                    errs.append(f"{at}: id 重复")
                seg_ids.add(s.id)
                if s.source not in self.sources:
                    errs.append(f"{at}: source={s.source!r} 不在 sources 里；可用：{list(self.sources)}")
                    continue
                src = self.sources[s.source]
                if s.action not in ACTIONS:
                    errs.append(f"{at}: action={s.action!r}，应为 {ACTIONS}")
                if s.reason not in REASONS:
                    errs.append(f"{at}: reason={s.reason!r}，应为 {list(REASONS)} 之一")
                if src.kind != "image" and not s.end > s.start:
                    errs.append(f"{at}: end ({s.end}) 必须大于 start ({s.start})")
                if s.start < 0:
                    errs.append(f"{at}: start ({s.start}) 不能为负")
                if src.duration and src.kind != "image" and s.end > src.duration + 0.05:
                    errs.append(f"{at}: end ({s.end}) 超出素材时长 {src.duration}")
                if src.kind == "image" and s.slot is None:
                    errs.append(f"{at}: 图片片段必须给出 slot（显示时长，秒）")
                if not 0.25 <= s.speed <= 4:
                    errs.append(f"{at}: speed={s.speed} 超出 [0.25, 4]")
                if not 0 <= s.confidence <= 1:
                    errs.append(f"{at}: confidence={s.confidence} 应在 [0, 1]")
                if s.slot is not None and s.slot <= 0:
                    errs.append(f"{at}: slot={s.slot} 必须为正")
            for bi, b in enumerate(o.beds):
                if b.source not in self.sources:
                    errs.append(f"outputs[{oi}].beds[{bi}]: source={b.source!r} 不在 sources 里")
            for xi, x in enumerate(o.overlays):
                if x.kind == "image" and x.source not in self.sources:
                    errs.append(f"outputs[{oi}].overlays[{xi}]: source={x.source!r} 不在 sources 里")
        if errs:
            raise PlanError("方案有 %d 处问题：\n- " % len(errs) + "\n- ".join(errs))


def _build(cls: type, d: dict[str, Any]) -> Any:
    """按 dataclass 字段构造；多余字段报错（拼错的字段名不能被静默忽略）。"""
    names = {f.name for f in fields(cls)}
    extra = set(d) - names
    if extra:
        raise PlanError(f"{cls.__name__} 不认识字段 {sorted(extra)}；可用字段：{sorted(names - {'path'})}")
    return cls(**d)


def _prune(x: Any) -> Any:
    """去掉值为默认空的字段，让方案文件更短、更好读（对 agent 也更省 token）。"""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            v = _prune(v)
            if v is None or v == [] or v == {} or v == "":
                continue
            out[k] = v
        return out
    if isinstance(x, list):
        return [_prune(v) for v in x]
    return x


def new_segment_id(prefix: str, n: int) -> str:
    return f"{prefix}{n:03d}"
