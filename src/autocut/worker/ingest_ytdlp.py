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
import subprocess
import sys
import time
from pathlib import Path

# 优先 H.264：AV1 的 mp4 合并有时会产出"视频流在中途断掉"的坏文件（2026-10-31 实测：视频 151s、音频 279s），而且 H.264 解码最快
AVC_FORMAT = ("bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/bestvideo[vcodec^=avc1]+bestaudio/"
              "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best")
ORIGINAL_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"


def _durations(path: Path) -> dict[str, float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    out: dict[str, float] = {}
    for st in json.loads(r.stdout or "{}").get("streams", []):
        try:
            out[st["codec_type"]] = max(out.get(st["codec_type"], 0.0), float(st["duration"]))
        except (KeyError, ValueError):
            pass
    return out


def integrity_problem(path: Path) -> str | None:
    """下载下来的文件能不能用：音视频时长要接近，末尾要能解出一帧。返回问题描述，没问题返回 None。"""
    d = _durations(path)
    v, a = d.get("video", 0.0), d.get("audio", 0.0)
    if not v:
        return "没有视频流"
    if a and abs(v - a) > max(3.0, 0.03 * max(v, a)):
        return f"视频流 {v:.0f}s 与音频流 {a:.0f}s 对不上（文件残缺）"
    t = max(0.0, min(v, a or v) - 2.0)
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or "Output file is empty" in r.stderr:
        return f"末尾 {t:.0f}s 处解不出画面"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta", required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    out = sys.stdout
    sys.stdout = sys.stderr  # yt-dlp / ffmpeg 会往 stdout 打进度，结果 JSON 只走真正的 stdout
    t0 = time.perf_counter()
    try:
        from yt2bili import download as D  # noqa: PLC0415 —— 只在烤肉 venv 里存在

        r = None
        for attempt, fmt in enumerate((AVC_FORMAT, ORIGINAL_FORMAT)):
            D._DASH_FORMAT = fmt  # 只改本进程里的模块变量，不动烤肉的代码
            r = D.download_video(a.url, Path(a.out), None, None)
            problem = integrity_problem(r.video_path)
            if not problem:
                break
            logging.warning("下载的文件有问题（%s），删除后换格式重下（第 %d 次）", problem, attempt + 1)
            r.video_path.unlink(missing_ok=True)
            if attempt == 1:
                raise RuntimeError(f"下载的文件残缺且重试仍失败：{problem}")
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"code": "download_failed", "message": f"{type(e).__name__}: {e}"[:600],
                                                  "fix": "换链接，或稍后重试；YouTube 反爬问题见烤肉的 cookies / PO-Token 服务", "retry": True}}, ensure_ascii=False), file=out)
        return 1
    meta = {
        "ok": True, "video_path": str(r.video_path), "video_id": r.video_id, "title": r.title, "channel": r.channel,
        "description": (r.description or "")[:2000], "live_status": r.live_status,
        "subtitle_path": str(r.subtitle_path) if r.subtitle_path else None,
        "thumbnail_path": str(r.thumbnail_path) if r.thumbnail_path else None,
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }
    Path(a.meta).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
