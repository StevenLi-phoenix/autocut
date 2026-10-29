"""evals/quarantine_tmp.py：只移动「被 transcript 引用 + 在 case 开始后修改过 + 不在保护名单」的 /tmp 顶层条目。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import quarantine_tmp as Q  # noqa: E402


def transcript(path: Path, commands: list[str]) -> Path:
    events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}
              for c in commands]
    path.write_text("\n".join(json.dumps(e) for e in events))
    return path


def test_moves_only_referenced_fresh_unprotected_entries(tmp_path):
    root, dest = tmp_path / "tmp", tmp_path / "dest"
    root.mkdir()
    for name in ("gc_work", "old_dir", "claude-501", "unrelated", "script.py"):
        (root / name).mkdir() if name != "script.py" else (root / name).write_text("x")
    since = time.time()
    os.utime(root / "old_dir", (since - 3600, since - 3600))  # 比 case 开始更早：不是这个 case 的
    t = transcript(tmp_path / "c.jsonl", [
        "mkdir -p /tmp/gc_work && cd /tmp/gc_work",
        "ls /tmp/old_dir /tmp/claude-501",                       # 被引用但分别是旧的 / 受保护的
        "python3 /tmp/script.py; cat /private/tmp/claude-501/x",  # /private/tmp/... 不是顶层 /tmp 引用
    ])
    assert Q.referenced_names([t]) == {"gc_work", "old_dir", "claude-501", "script.py"}
    moved = Q.quarantine(Q.referenced_names([t]), since, dest, root)
    assert moved == ["gc_work", "script.py"]
    assert (dest / "gc_work").is_dir() and (dest / "script.py").is_file()
    assert (root / "old_dir").exists() and (root / "claude-501").exists() and (root / "unrelated").exists()
    assert not (root / "gc_work").exists()


def test_missing_transcript_and_missing_entries_are_ignored(tmp_path):
    assert Q.referenced_names([tmp_path / "nope.jsonl"]) == set()
    assert Q.quarantine({"ghost"}, time.time(), tmp_path / "d", tmp_path) == []
