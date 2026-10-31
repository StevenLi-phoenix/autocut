"""worker 配置：全部来自环境变量，便于 launchd plist 与测试覆盖。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = "/Volumes/MiniSSD/autocut"
DEFAULT_PORT = 8102
RESERVED_NAMES = {"models", "_worker", "inbox"}  # 不能当 job 名


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
        )
