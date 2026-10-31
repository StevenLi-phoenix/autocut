"""确定性的眼睛：拼图、响度曲线、没有任何 LLM。"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import numpy as np
import pytest

from autocut.worker import signals

pytestmark = pytest.mark.unit
WORKER = Path(__file__).resolve().parent.parent / "src" / "autocut" / "worker"


def test_contact_sheet_and_frames_brief(tmp_path: Path) -> None:
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=20:s=640x360:r=25", "-pix_fmt", "yuv420p", str(v)], check=True)
    sheets = signals.frames_brief(v, [1.0, 5.0, 9.0, 13.0, 17.0], tmp_path / "f", "t", per_sheet=4)
    assert len(sheets) == 2 and [len(s["cells"]) for s in sheets] == [4, 1]
    assert all(Path(s["path"]).stat().st_size > 2000 for s in sheets) and sheets[0]["cells"][1]["label"] == "2 00:05"
    assert not list((tmp_path / "f").glob(".*.jpg"))  # 临时单帧已清理


def test_loudness_curve() -> None:
    sr = 8000
    x = np.concatenate([np.zeros(sr * 5), 0.5 * np.sin(np.arange(sr * 5) / 10)]).astype("float32")
    c = signals.loudness_curve(x, sr, 5.0)
    assert len(c) == 2 and c[0] < -80 < -10 < c[1] + 100 and c[1] > -15


def test_ocr_without_binary_is_empty(tmp_path: Path) -> None:
    assert signals.ocr([tmp_path / "x.jpg"], None) == {}


def test_worker_has_no_llm_calls() -> None:
    """autocut 里不能有任何 LLM 调用（用户 2026-10-31 的决定）：worker 代码里没有网关/模型/评审的踪迹。"""
    banned = ("chat/completions", "llmgateway", "AUTOCUT_LLM_KEY", "claude-", "deepseek-", "image_url", "api.lishuyu.app/llm")
    for p in WORKER.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        ast.parse(src)
        for b in banned:
            assert b not in src, f"{p.name} 含 {b!r}"
    assert not (WORKER / "llm.py").exists()
