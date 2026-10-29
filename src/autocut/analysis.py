"""分析门面：对一个素材做各种信号分析，并把结果缓存到磁盘（重跑配方、调参数时不必重新分析）。

缓存目录：环境变量 AUTOCUT_CACHE，否则是素材旁边的 .autocut-cache/。
缓存键 = 素材路径 + 大小 + 修改时间 + 分析参数；素材变了缓存自动失效。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict
from functools import cached_property
from pathlib import Path
from typing import Any, Callable

import numpy as np

from autocut.audio import load_audio
from autocut.beats import BeatResult, detect_beats
from autocut.probe import MediaInfo, probe
from autocut.scenes import detect_cuts
from autocut.speech import SpeechResult, detect_speech

log = logging.getLogger(__name__)

SR = 16000
# 音轨标题里出现这些词，就认为是人声轨（OBS 常见命名：Mic、Voice、麦克风……）
_VOICE_TITLE = re.compile(r"mic|voice|vocal|narrat|commentary|麦|人声|解说|旁白", re.I)
_MIX_TITLE = re.compile(r"mix|master|混|全部", re.I)


def cache_root(near: Path) -> Path:
    env = os.environ.get("AUTOCUT_CACHE")
    return Path(env) if env else near.parent / ".autocut-cache"


class Media:
    """一个素材文件的分析入口。所有 public 方法的结果都可安全复用（内存 + 磁盘缓存）。"""

    def __init__(self, path: str | Path, cache_dir: Path | None = None) -> None:
        self.path = Path(path).resolve()
        if not self.path.exists():
            raise FileNotFoundError(f"找不到素材文件：{self.path}")
        st = self.path.stat()
        key = hashlib.sha1(f"{self.path}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
        self.cache_dir = (cache_dir or cache_root(self.path)) / f"{self.path.stem[:40]}-{key}"
        self._audio: dict[tuple[int | None, int], np.ndarray] = {}

    # ---- 基础信息 ----
    @cached_property
    def info(self) -> MediaInfo:
        return probe(self.path)

    @property
    def duration(self) -> float:
        return self.info.duration

    def voice_track(self) -> int | None:
        """猜一条人声轨供分析用：标题像人声的优先；否则多轨时选第 2 条（OBS 默认 1=混音、其余分轨）不可靠，返回 None。"""
        tracks = self.info.audio
        if len(tracks) <= 1:
            return None
        for t in tracks:
            if t.title and _VOICE_TITLE.search(t.title):
                return t.track_number
        return None

    def mix_track(self) -> int | None:
        for t in self.info.audio:
            if t.title and _MIX_TITLE.search(t.title):
                return t.track_number
        return None

    def track_label(self, track: int | None) -> str:
        if track is None:
            return "默认音轨"
        for t in self.info.audio:
            if t.track_number == track:
                return f"音轨 {track}" + (f"（{t.title}）" if t.title else "")
        return f"音轨 {track}"

    # ---- 音频与信号 ----
    def audio(self, track: int | None = None, sr: int = SR) -> np.ndarray:
        k = (track, sr)
        if k not in self._audio:
            t0 = time.perf_counter()
            self._audio[k] = load_audio(self.path, track=track, sr=sr)
            log.debug("解码 %s %s @%d：%.2fs", self.path.name, self.track_label(track), sr, time.perf_counter() - t0)
        return self._audio[k]

    def speech(self, track: int | None = None, **kw: Any) -> SpeechResult:
        def compute() -> dict:
            return asdict(detect_speech(self.audio(track), SR, **kw))

        d = self._cached(f"speech-t{track}-{_kw(kw)}", compute)
        d["regions"] = tuple(tuple(r) for r in d["regions"])
        return SpeechResult(**d)

    def beats(self) -> BeatResult:
        d = self._cached("beats", lambda: asdict(detect_beats(self.audio(sr=22050), 22050)))
        return BeatResult(bpm=d["bpm"], beats=tuple(d["beats"]), downbeats=tuple(d["downbeats"]), confidence=d["confidence"])

    def cuts(self) -> list[float]:
        if self.info.video is None:
            return []
        return self._cached("cuts", lambda: detect_cuts(self.path))

    def words(self, language: str | None = None, track: int | None = None) -> list[dict[str, Any]] | None:
        """逐词文字稿 [{"text","start","end","p"}...]；没有可用的语音识别时返回 None（调用方要降级并告知用户）。"""
        try:
            from autocut import transcribe
        except ImportError:
            return None
        if not transcribe.available():
            return None
        return self._cached(
            words_cache_name(language, track),
            lambda: transcribe.transcribe(self, language=language, track=track),
        )

    def words_cache_path(self, language: str | None = None, track: int | None = None) -> Path | None:
        try:
            p = self.cache_dir / f"{words_cache_name(language, track)}.json"
        except ImportError:
            return None
        return p if p.exists() else None

    # ---- 缓存 ----
    def _cached(self, name: str, compute: Callable[[], Any]) -> Any:
        p = self.cache_dir / f"{name}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                log.warning("缓存损坏，重新计算：%s", p)
        t0 = time.perf_counter()
        val = compute()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(val, ensure_ascii=False, default=_jsonable))
        tmp.replace(p)
        log.info("分析 %s · %s：%.2fs（已缓存）", self.path.name, name, time.perf_counter() - t0)
        return json.loads(p.read_text())


def words_cache_name(language: str | None, track: int | None) -> str:
    """文字稿缓存名：语言 + 音轨 + 模型 + 解析版本。换模型或改了解析逻辑，旧缓存自动不再命中。"""
    from autocut import transcribe

    return f"words-{language or 'auto'}-t{track}-{transcribe.model_tag()}-p{transcribe.PARSE_VERSION}"


def _kw(kw: dict[str, Any]) -> str:
    return "-".join(f"{k}{v}" for k, v in sorted(kw.items())) or "default"


def _jsonable(x: Any) -> Any:
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    raise TypeError(f"无法序列化 {type(x)}")
