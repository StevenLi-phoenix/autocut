"""worker 配置：全部来自环境变量，便于 launchd plist 与测试覆盖。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = "/Volumes/MiniSSD/autocut"
DEFAULT_PORT = 8102
RESERVED_NAMES = {"models", "_worker", "inbox"}  # 不能当 job 名


def load_secrets(path: str | None) -> dict[str, str]:
    """读 KEY=VALUE 的 secrets 文件。值只在内存里，不记日志。"""
    out: dict[str, str] = {}
    if not path:
        return out
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


@dataclass(frozen=True)
class WorkerConfig:
    root: Path
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"
    autocut_cmd: tuple[str, ...] = (sys.executable, "-m", "autocut.cli")
    ffmpeg_dir: str | None = None  # 私有 ffmpeg 目录，放 PATH 最前
    encoder: str = "libx264"  # 与烤肉共用 VideoToolbox，worker 默认走软编并降低优先级
    heavy_slots: int = 1
    nice: int = 10
    ingest_python: str | None = None  # 烤肉 venv 的 python（复用其 yt-dlp / PO-Token / cookies 配置）
    ingest_env_file: str | None = None  # 烤肉 .env；只摘取白名单变量，不整体透传
    secrets_file: str | None = None  # KEY=VALUE 文件（600）：肥鱼 key、DSH 事件 token，不进 plist、不进日志
    ocr_bin: str | None = None  # 编译好的 vision_ocr（烤肉仓库 scripts/vision_ocr.swift，Apple Vision，离线）；没配就不做 OCR
    tts_mini_url: str = "http://127.0.0.1:8098"
    tts_feiyu_url: str | None = None  # 肥鱼（laptop）；mini 到 laptop 19880 不通时自动回落 mini TTS
    tts_order: tuple[str, ...] = ("feiyu", "say")  # mini 的 Qwen3-TTS 已被退役（dsh-voice 2026-10-31），需要时用 AUTOCUT_TTS_ORDER=feiyu,mini,say
    sensitive_words_file: str | None = None  # 烤肉的 sensitive_words.txt（一行一个）
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WorkerConfig:
        e = os.environ if env is None else env
        cmd = e.get("AUTOCUT_WORKER_BIN")
        return cls(
            root=Path(e.get("AUTOCUT_WORKER_ROOT", DEFAULT_ROOT)),
            port=int(e.get("AUTOCUT_WORKER_PORT", DEFAULT_PORT)),
            host=e.get("AUTOCUT_WORKER_HOST", "127.0.0.1"),
            autocut_cmd=tuple(cmd.split()) if cmd else (sys.executable, "-m", "autocut.cli"),
            ffmpeg_dir=e.get("AUTOCUT_WORKER_FFMPEG_DIR") or None,
            encoder=e.get("AUTOCUT_ENCODER", "libx264"),
            heavy_slots=int(e.get("AUTOCUT_WORKER_HEAVY_SLOTS", "1")),
            nice=int(e.get("AUTOCUT_WORKER_NICE", "10")),
            ingest_python=e.get("AUTOCUT_WORKER_INGEST_PYTHON") or None,
            ingest_env_file=e.get("AUTOCUT_WORKER_INGEST_ENV_FILE") or None,
            secrets_file=e.get("AUTOCUT_WORKER_SECRETS_FILE") or None,
            ocr_bin=e.get("AUTOCUT_WORKER_OCR_BIN") or None,
            tts_mini_url=e.get("AUTOCUT_TTS_MINI_URL", "http://127.0.0.1:8098"),
            tts_feiyu_url=e.get("AUTOCUT_TTS_FEIYU_URL") or None,
            sensitive_words_file=e.get("AUTOCUT_WORKER_SENSITIVE_WORDS") or None,
            tts_order=tuple(x for x in e.get("AUTOCUT_TTS_ORDER", "feiyu,say").split(",") if x),
        )
