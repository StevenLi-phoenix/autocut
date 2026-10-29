#!/usr/bin/env python
"""把一个 eval case 在 /tmp 顶层用过的路径移到隔离目录，避免后面的 case 读到它的残留。

/tmp 是并发 case 共用的，Seatbelt 沙盒不管它（agent 习惯把脚本和中间文件写到 /tmp/talkclean、/tmp/gc_work 这类固定名字）。
只移动同时满足：被该 case 的 transcript 里某个工具调用引用、修改时间 ≥ 该 case 开始时间、名字不在保护名单里的顶层条目。

用法: quarantine_tmp.py --since <epoch秒> --dest <目录> <transcript.jsonl>...
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from pathlib import Path

log = logging.getLogger("quarantine_tmp")
PROTECTED = re.compile(r"^(claude|cc-|com\.|cna\.|bun-|drivefs|node-|npm-|pytest|ssh-|launch|\.|__)")
REF = re.compile(r"(?<![A-Za-z0-9_./-])/tmp/([A-Za-z0-9_.\-]+)")


def referenced_names(transcripts: list[Path]) -> set[str]:
    names: set[str] = set()
    for t in transcripts:
        if not t.exists():
            continue
        for line in t.read_text(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "assistant":
                continue
            for b in ev.get("message", {}).get("content") or []:
                if b.get("type") == "tool_use":
                    names.update(REF.findall(json.dumps(b.get("input") or {}, ensure_ascii=False)))
    return names


def quarantine(names: set[str], since: float, dest: Path, tmp_root: Path = Path("/tmp")) -> list[str]:
    moved = []
    for n in sorted(names):
        p = tmp_root / n
        if PROTECTED.match(n) or not p.exists() and not p.is_symlink():
            continue
        if p.lstat().st_mtime < since - 1:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest / n))
        moved.append(n)
    return moved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", type=float, required=True)
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("transcripts", nargs="+", type=Path)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    moved = quarantine(referenced_names(a.transcripts), a.since, a.dest)
    log.info("quarantined %d /tmp entries: %s", len(moved), " ".join(moved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
