"""TTS 客户端：多后端（肥鱼 / mini Qwen3-TTS）按顺序回落，成批合成，按内容缓存。

- 缓存键 = sha256(provider|voice|language|text)，跨 job 共享（<root>/_worker/tts-cache/）：同一句不会让 laptop 再加载一次模型。
- 肥鱼每次加载占 laptop ~7.8GB：调用方应一次把整批句子发完（本模块按批取走缓存未命中的），失败时重试并回落到 mini。
- 原始响应（音频字节）先写盘，再读时长。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

from autocut.worker.config import WorkerConfig, load_secrets

log = logging.getLogger(__name__)


class TTSError(Exception):
    pass


def _multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except wave.Error:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
        return float(out.stdout.strip())


class TTS:
    def __init__(self, cfg: WorkerConfig, cache_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.cache = cache_dir or (cfg.root / "_worker" / "tts-cache")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.feiyu_key = load_secrets(cfg.secrets_file).get("AUTOCUT_FEIYU_KEY") or ""
        self.stats = {"hit": 0, "miss": 0, "provider": {}}

    def providers(self) -> list[str]:
        out = []
        for p in self.cfg.tts_order:
            if p == "feiyu" and self.cfg.tts_feiyu_url and self.feiyu_key:
                out.append(p)
            elif p in ("mini", "say"):
                out.append(p)
        return out

    def key(self, provider: str, voice: str, lang: str, text: str) -> Path:
        h = hashlib.sha256(f"{provider}|{voice}|{lang}|{text}".encode()).hexdigest()[:32]
        return self.cache / f"{provider}-{h}.wav"

    def synth_many(self, lines: list[str], *, voice: str = "vivian", lang: str = "Chinese") -> list[tuple[Path, float, str]]:
        """逐句合成（缓存优先）。返回 [(wav 路径, 秒数, provider)]。某个后端整批失败就换下一个后端。"""
        results: list[tuple[Path, float, str] | None] = [None] * len(lines)
        for provider in self.providers():
            todo = [i for i, r in enumerate(results) if r is None]
            if not todo:
                break
            # 先吃缓存
            for i in list(todo):
                p = self.key(provider, voice, lang, lines[i])
                if p.exists() and p.stat().st_size > 1000:
                    results[i] = (p, wav_duration(p), provider)
                    self.stats["hit"] += 1
            todo = [i for i, r in enumerate(results) if r is None]
            failed = 0
            for i in todo:
                p = self.key(provider, voice, lang, lines[i])
                try:
                    if p.exists() and p.stat().st_size > 1000:  # 同一批里的重复句：前一句刚合成过
                        results[i] = (p, wav_duration(p), provider)
                        self.stats["hit"] += 1
                        continue
                    self._one(provider, lines[i], voice, lang, p)
                    results[i] = (p, wav_duration(p), provider)
                    self.stats["miss"] += 1
                    self.stats["provider"][provider] = self.stats["provider"].get(provider, 0) + 1
                except TTSError as e:
                    failed += 1
                    log.warning("TTS %s 第 %d 句失败：%s", provider, i, e)
                    if failed >= 2:  # 后端明显不可用，别逐句耗时间，整批回落
                        break
        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            raise TTSError(f"{len(missing)} 句没能合成（后端：{self.providers()}）")
        return [r for r in results if r is not None]

    def _one(self, provider: str, text: str, voice: str, lang: str, dst: Path) -> None:
        last: Exception | None = None
        for attempt in range(3):
            t0 = time.perf_counter()
            try:
                if provider == "say":
                    data = self._say(text, lang)
                elif provider == "mini":
                    body, ct = _multipart({"text": text, "speaker": voice, "language": lang})
                    req = urllib.request.Request(f"{self.cfg.tts_mini_url}/synthesize", data=body, method="POST", headers={"Content-Type": ct})
                    with urllib.request.urlopen(req, timeout=300) as r:
                        data = r.read()
                else:
                    data = self._feiyu(text, voice, lang)
                tmp = dst.with_suffix(".part")
                tmp.write_bytes(data)  # 原始响应先落盘
                if len(data) < 1000:
                    raise TTSError(f"返回的音频太小（{len(data)} 字节）")
                tmp.replace(dst)
                log.info("TTS %s %d 字 → %s %.1fs", provider, len(text), dst.name, time.perf_counter() - t0)
                return
            except (urllib.error.URLError, TimeoutError, OSError, TTSError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise TTSError(f"{provider}：{last}")

    def _say(self, text: str, lang: str) -> bytes:
        """macOS 系统语音（最后的退路，机械感明显；调用方必须在摘要里标注降级）。"""
        v = "Tingting" if lang.lower().startswith("chin") else "Samantha"
        tmp = self.cache / f".say-{uuid.uuid4().hex}.aiff"
        out = tmp.with_suffix(".wav")
        try:
            subprocess.run(["say", "-v", v, "-o", str(tmp), "--", text], check=True, capture_output=True, timeout=120)
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp), "-ac", "1", "-ar", "24000", str(out)], check=True, capture_output=True, timeout=120)
            return out.read_bytes()
        except (subprocess.SubprocessError, OSError) as e:
            raise TTSError(f"say 失败：{e}") from e
        finally:
            tmp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    def _feiyu(self, text: str, voice: str, lang: str) -> bytes:
        # 肥鱼 v0.4.0 的 HTTP 契约以服务文档为准；这里按 submit → wait → download 的最小形式留好接口，mini 不通时不会走到这里
        raise TTSError("肥鱼后端在 mini 上未接通（mini 到 laptop:19880 不通），走 mini TTS")


def atempo_chain(speed: float) -> str:
    """ffmpeg atempo 允许 0.5–2.0；这里只用 1.0–1.3。"""
    return f"atempo={speed:.4f}"
