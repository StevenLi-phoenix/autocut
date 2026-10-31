"""ingest：登记素材来源（provenance），下载 URL（复用烤肉）、接收上传、取 inbox 文件。

每个任务记录素材来源；是否标注转载由 job.config.attribution 决定（用户每次任务指定）。
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from autocut.worker import policy
from autocut.worker.config import WorkerConfig
from autocut.worker.store import Store, atomic_write_json, read_json

log = logging.getLogger(__name__)
MEDIA_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".png", ".jpg", ".jpeg", ".srt", ".md", ".txt", ".json"}
# 只从烤肉 .env 摘这些变量给 yt-dlp（不透传 LLM key、内部密钥等）
YT_ENV_WHITELIST = ("KAOROU_YT_COOKIES_PATH", "KAOROU_POT_BASE_URL", "KAOROU_YT_FORCE_IPV4", "KAOROU_YT_IMPERSONATE",
                    "KAOROU_YT_COOKIES_BROWSER", "KAOROU_JS_RUNTIME_DIR")


def parse_env_file(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    """读 KEY=VALUE（支持引号），只取白名单键。值不进日志。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("读不到 ingest env 文件 %s：%s", path, e)
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        if k in keys:
            try:
                parts = shlex.split(v, posix=True)
            except ValueError:
                parts = [v.strip()]
            out[k] = parts[0] if parts else ""
    return out


def ingest_env(cfg: WorkerConfig) -> dict[str, str]:
    env = {"PATH": os.pathsep.join(p for p in (cfg.ffmpeg_dir, "/opt/homebrew/bin", "/usr/bin", "/bin") if p),
           "HOME": os.environ.get("HOME", "/tmp"), "KAOROU_MIN_HEIGHT": "360", "PYTHONUNBUFFERED": "1"}
    if cfg.ingest_env_file:
        env.update(parse_env_file(Path(cfg.ingest_env_file), YT_ENV_WHITELIST))
    return env


def probe(path: Path) -> dict[str, Any]:
    """ffprobe 取时长/分辨率/音轨数；失败返回空（登记不因此失败）。"""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=30)
        import json

        j = json.loads(p.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        log.warning("ffprobe 失败 %s：%s", path, e)
        return {}
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = sum(1 for s in j.get("streams", []) if s.get("codec_type") == "audio")
    out: dict[str, Any] = {"audio_tracks": a}
    if j.get("format", {}).get("duration"):
        out["duration_s"] = round(float(j["format"]["duration"]), 2)
    if v:
        out["width"], out["height"] = v.get("width"), v.get("height")
    return out


def register_source(store: Store, job_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """追加一条素材来源到 provenance.json，返回登记后的条目（含 id）。"""
    d = store.job_dir(job_id)
    prov = read_json(d / "provenance.json", {"sources": []})
    entry = {"id": f"s{len(prov['sources']) + 1}", "registered_at": time.time(), **entry}
    prov["sources"].append(entry)
    atomic_write_json(d / "provenance.json", prov)
    log.info("素材登记：%s %s", job_id, {k: entry.get(k) for k in ("id", "file", "kind", "url", "title")})
    return entry


def credits_text(store: Store, job_id: str) -> str:
    """按 job 配置生成转载/来源标注文字；配置为不标注时返回空串。"""
    job = store.get_job(job_id)
    attr = (job.get("config") or {}).get("attribution") or {}
    if not attr.get("repost_label"):
        return ""
    prov = read_json(store.job_dir(job_id) / "provenance.json", {"sources": []})
    lines = []
    for s in prov["sources"]:
        if s.get("kind") != "url":
            continue
        who = s.get("channel") or s.get("title") or "原作者"
        lines.append(f"转载自 {who}《{s.get('title', '')}》 原链接：{s['url']}")
    custom = attr.get("text")
    return "\n".join([*lines, *([custom] if custom else [])])


def from_inbox(cfg: WorkerConfig, store: Store, job_id: str, rel: str, kind: str = "inbox") -> dict[str, Any]:
    """把 <root>/inbox/ 下的文件复制进 job 的 src/。inbox 以外的路径一律拒绝。"""
    inbox = (cfg.root / "inbox").resolve()
    src = (inbox / rel).resolve()
    try:
        src.relative_to(inbox)
    except ValueError:
        raise policy.PolicyError("path_outside_inbox", f"只能取 inbox 下的文件：{rel}", f"先把文件放到 {inbox}，或用 PUT /v1/jobs/<job>/upload 上传") from None
    if not src.is_file() or src.suffix.lower() not in MEDIA_EXT:
        raise policy.PolicyError("bad_inbox_file", f"inbox 里没有可用文件：{rel}", "检查文件名和扩展名")
    dst = store.job_dir(job_id) / "src" / src.name
    shutil.copy2(src, dst)
    return register_source(store, job_id, {"kind": kind, "file": f"src/{dst.name}", "origin": f"inbox/{rel}", **probe(dst)})
