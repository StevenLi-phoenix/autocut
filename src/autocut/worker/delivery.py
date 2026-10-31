"""成片确认状态机 + 预览 + 交付。

状态：working → awaiting_confirmation → confirmed → handed_off；被拒走 rejected（之后重新 working）。
**handoff 只在 confirmed 之后可用**——这是硬门，不靠 agent 自觉。confirm 必须附用户原话，写进审计记录。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from autocut.worker import ingest, policy
from autocut.worker.policy import PolicyError
from autocut.worker.store import Store, atomic_write_json, read_json

log = logging.getLogger(__name__)
PREVIEW_MAX_BYTES = 23 * 1024 * 1024  # 微信文件上限 25 MiB，留余量
REASONS = {"length", "standalone", "boring", "irrelevant", "captions", "copyright", "other"}


def state(store: Store, job_id: str) -> dict[str, Any]:
    return read_json(store.job_dir(job_id) / "state.json", {"status": "working", "history": [], "deliverables": []})


def _save(store: Store, job_id: str, st: dict[str, Any], event: str, **info: Any) -> dict[str, Any]:
    st["history"].append({"t": time.time(), "event": event, **info})
    atomic_write_json(store.job_dir(job_id) / "state.json", st)
    log.info("job %s 状态 → %s %s", job_id, st["status"], {k: v for k, v in info.items() if k != "quote"})
    return st


def mark_awaiting(store: Store, job_id: str, deliverables: list[dict[str, Any]], summary: str) -> dict[str, Any]:
    st = state(store, job_id)
    st.update(status="awaiting_confirmation", deliverables=deliverables, summary=summary)
    return _save(store, job_id, st, "awaiting_confirmation", n=len(deliverables))


def confirm(store: Store, job_id: str, quote: str) -> dict[str, Any]:
    st = state(store, job_id)
    if st["status"] != "awaiting_confirmation":
        raise PolicyError("not_awaiting", f"当前状态是 {st['status']}，没有等待确认的成片", "先让流水线产出成片（status 里 state=awaiting_confirmation）再确认")
    if len((quote or "").strip()) < 2:
        raise PolicyError("quote_required", "confirm 必须带用户确认的原话", "--quote '用户的原话'；不要替用户确认")
    st["status"] = "confirmed"
    return _save(store, job_id, st, "confirmed", quote=quote.strip()[:500])


def reject(store: Store, job_id: str, reason: str, text: str, outputs: list[str]) -> dict[str, Any]:
    st = state(store, job_id)
    if st["status"] not in ("awaiting_confirmation", "confirmed"):
        raise PolicyError("not_awaiting", f"当前状态是 {st['status']}，没有可拒绝的成片", "")
    if reason not in REASONS:
        raise PolicyError("bad_reason", f"原因码不认识：{reason}", f"可选：{'、'.join(sorted(REASONS))}")
    d = store.job_dir(job_id)
    rej = read_json(d / "rejections.json", {"items": []})
    rej["items"].append({"t": time.time(), "reason": reason, "text": text[:500], "outputs": outputs})
    atomic_write_json(d / "rejections.json", rej)
    st["status"] = "rejected"
    return _save(store, job_id, st, "rejected", reason=reason, outputs=outputs)


def make_preview(src: Path, dst: Path, *, ffmpeg: str = "ffmpeg", max_bytes: int = PREVIEW_MAX_BYTES) -> dict[str, Any]:
    """压一个 ≤max_bytes 的确认用预览（微信文件上限 25 MiB）。成片本身不动。"""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)], capture_output=True, text=True, check=True).stdout.strip())
    audio = 64_000
    vbr = int(max_bytes * 8 * 0.9 / max(dur, 1.0)) - audio
    if vbr < 120_000:
        raise PolicyError("preview_too_long", f"成片 {dur:.0f}s 太长，压不进 {max_bytes // 2**20} MiB 预览", "改发片段预览或截图，并告诉用户完整成片在 mini 上")
    short_side = 720 if vbr >= 1_000_000 else 540 if vbr >= 500_000 else 360
    # 横屏限高、竖屏限宽，保持比例，偶数边长
    vf = f"scale='if(gt(iw,ih),-2,min({short_side},iw))':'if(gt(iw,ih),min({short_side},ih),-2)'"
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vbr),
           "-maxrate", str(int(vbr * 1.4)), "-bufsize", str(vbr * 2), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-movflags", "+faststart", str(dst)]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
    size = dst.stat().st_size
    log.info("预览：%s → %s %.1fMiB v=%dk %.1fs", src.name, dst.name, size / 2**20, vbr // 1000, time.perf_counter() - t0)
    if size > max_bytes + 2 * 2**20:
        raise PolicyError("preview_too_big", f"预览 {size / 2**20:.1f} MiB 超限", "降低码率重试")
    return {"path": str(dst), "bytes": size, "video_kbps": vbr // 1000}


def handoff(store: Store, job_id: str, to: str, title: str, desc: str, root: Path) -> dict[str, Any]:
    st = state(store, job_id)
    if st["status"] not in ("confirmed", "handed_off"):
        raise PolicyError("not_confirmed", f"成片还没有用户确认（状态：{st['status']}），不能交付或上传", "先把预览发给用户，拿到明确同意后 confirm --quote '用户原话'")
    if to != "local":
        raise PolicyError("handoff_target_unsupported", f"暂不支持的交付目标：{to}", "目前只有 local（落盘到 <root>/handoff/）")
    d = store.job_dir(job_id)
    dest = root / "handoff" / job_id
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for dv in st.get("deliverables", []):
        src = policy.inside(d, dv["final"])
        shutil.copy2(src, dest / src.name)
        files.append(str(dest / src.name))
        for extra in dv.get("extras", []):
            ep = policy.inside(d, extra)
            if ep.is_file():
                shutil.copy2(ep, dest / ep.name)
    credits = ingest.credits_text(store, job_id)
    meta = {"job": job_id, "title": title, "desc": desc, "credits": credits, "files": files, "handed_off_at": time.time(), "provenance": read_json(d / "provenance.json", {})}
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if credits:
        (dest / "credits.txt").write_text(credits + "\n", encoding="utf-8")
    st["status"] = "handed_off"
    _save(store, job_id, st, "handed_off", to=to, files=files)
    return {"ok": True, "to": to, "dir": str(dest), "files": files, "credits": credits}
