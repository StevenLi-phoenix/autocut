"""信号分析模块测试的公共夹具。

素材在 fixtures/media/（gitignored，由 fixtures/make_fixtures.py 生成），缺失时集成测试整体 skip。
marker 在这里注册，避免改动 pyproject.toml。
"""

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: 合成信号的纯 numpy 单元测试，不依赖素材")
    config.addinivalue_line("markers", "integration: 依赖 ffmpeg 与 fixtures/media 素材的集成测试")


@pytest.fixture(scope="session")
def media() -> Path:
    """fixtures/media 目录；素材缺失时 skip。"""
    if not (MEDIA / "talk_zh.mp4").exists():
        pytest.skip("run fixtures/make_fixtures.py")
    return MEDIA


@pytest.fixture(scope="session")
def truth(media: Path) -> Callable[[str], dict]:
    """按名字读取 <name>.truth.json。"""

    def _load(name: str) -> dict:
        return json.loads((media / f"{name}.truth.json").read_text())

    return _load


@pytest.fixture(scope="session")
def audio_of(media: Path) -> Callable[..., np.ndarray]:
    """带缓存的 load_audio(相对 media 的路径, track, sr)，同一份音频只解码一次。"""
    from autocut.audio import load_audio

    cache: dict[tuple, np.ndarray] = {}

    def _load(rel: str, track: int | str | None = None, sr: int = 16000) -> np.ndarray:
        key = (rel, track, sr)
        if key not in cache:
            cache[key] = load_audio(media / rel, track=track, sr=sr)
        return cache[key]

    return _load


def run_ffmpeg(*args: str) -> None:
    """在测试里用 ffmpeg 构造输入；失败时带上 stderr，方便定位。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg {' '.join(args)} 失败：{r.stderr.strip()}")


def coverage(regions: list[tuple[float, float]] | tuple, a: float, b: float) -> float:
    """regions 与区间 [a, b] 的重叠总时长。"""
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in regions)


@pytest.fixture(autouse=True)
def _debug_logs(caplog: pytest.LogCaptureFixture) -> None:
    """让被测模块的 debug 日志进入失败报告（pytest -o log_cli=true 可实时看）。"""
    caplog.set_level(logging.DEBUG, logger="autocut")
