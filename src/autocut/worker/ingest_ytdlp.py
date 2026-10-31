"""在烤肉的 venv 里运行（复用 yt2bili.download：PO-Token、cookies 轮换、IPv4/IPv6 回退、DASH 阶梯）。

用法（由 worker 调用，不是给人用的）：
    <kaorou-python> ingest_ytdlp.py --url URL --out DIR --meta FILE
成功：把元数据 JSON 写到 --meta，stdout 打印同一份；失败：stdout 打印 {"ok": false, "error": ...}，退出码 1。
这个文件不 import autocut，也不改烤肉的任何代码。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta", required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.perf_counter()
    try:
        from yt2bili.download import download_video  # noqa: PLC0415 —— 只在烤肉 venv 里存在

        r = download_video(a.url, Path(a.out), None, None)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"code": "download_failed", "message": f"{type(e).__name__}: {e}"[:600],
                                                  "fix": "换链接，或稍后重试；YouTube 反爬问题见烤肉的 cookies / PO-Token 服务", "retry": True}}, ensure_ascii=False))
        return 1
    meta = {
        "ok": True, "video_path": str(r.video_path), "video_id": r.video_id, "title": r.title, "channel": r.channel,
        "description": (r.description or "")[:2000], "live_status": r.live_status,
        "subtitle_path": str(r.subtitle_path) if r.subtitle_path else None,
        "thumbnail_path": str(r.thumbnail_path) if r.thumbnail_path else None,
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }
    Path(a.meta).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
