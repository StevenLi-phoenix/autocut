"""成片确认状态机 + 预览 + 交付。

状态：working → awaiting_confirmation → confirmed → handed_off；被拒走 rejected（之后重新 working）。
**handoff 只在 confirmed 之后可用**——这是硬门，不靠 agent 自觉。confirm 必须附用户原话，写进审计记录。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from autocut.worker import ingest, policy
from autocut.worker.policy import PolicyError
from autocut.worker.store import Store, atomic_write_json, read_json

log = logging.getLogger(__name__)
# 微信文件上限 25 MiB，但 mini 到微信 CDN 的上行实测只有约 65 KB/s，DSH 的 wechat 插件每次上传 60 秒超时
# （2026-10-31：22 MiB 和 10 MiB 的预览都超时）。默认压到 8 MiB，可用环境变量 AUTOCUT_PREVIEW_MIB 调整。
PREVIEW_MAX_BYTES = int(float(os.environ.get("AUTOCUT_PREVIEW_MIB", "8")) * 1024 * 1024)
REASONS = {"length", "standalone", "boring", "irrelevant", "captions", "copyright", "other"}


def state(store: Store, job_id: str) -> dict[str, Any]:
    return read_json(store.job_dir(job_id) / "state.json", {"status": "working", "history": [], "deliverables": []})


def _save(store: Store, job_id: str, st: dict[str, Any], event: str, **info: Any) -> dict[str, Any]:
    st["history"].append({"t": time.time(), "event": event, **info})
    atomic_write_json(store.job_dir(job_id) / "state.json", st)
    log.info("job %s 状态 → %s %s", job_id, st["status"], {k: v for k, v in info.items() if k != "quote"})
    return st


def live(st: dict[str, Any]) -> list[dict[str, Any]]:
    """没被拒绝的成片。"""
    return [d for d in st.get("deliverables", []) if d.get("status") != "rejected"]


def mark_awaiting(store: Store, job_id: str, deliverables: list[dict[str, Any]], summary: str, *, carry: bool = False) -> dict[str, Any]:
    """停在等用户确认。carry=True：保留上一轮没被拒绝的成片（被拒后只补做被拒的那几条）。"""
    st = state(store, job_id)
    kept = live(st) if carry else []
    for d in kept:
        d["status"] = "carried"
    st.update(status="awaiting_confirmation", deliverables=[*kept, *deliverables], summary=summary)
    return _save(store, job_id, st, "awaiting_confirmation", n=len(st["deliverables"]), carried=len(kept))


def confirm(store: Store, job_id: str, quote: str) -> dict[str, Any]:
    st = state(store, job_id)
    if st["status"] != "awaiting_confirmation":
        raise PolicyError("not_awaiting", f"当前状态是 {st['status']}，没有等待确认的成片", "先让流水线产出成片（status 里 state=awaiting_confirmation）再确认")
    if len((quote or "").strip()) < 2:
        raise PolicyError("quote_required", "confirm 必须带用户确认的原话", "--quote '用户的原话'；不要替用户确认")
    st["status"] = "confirmed"
    _metric(store.root, job_id, "confirmed", n=len(live(st)))
    return _save(store, job_id, st, "confirmed", quote=quote.strip()[:500])


def reject(store: Store, job_id: str, reason: str, text: str, outputs: list[str]) -> dict[str, Any]:
    st = state(store, job_id)
    if st["status"] not in ("awaiting_confirmation", "confirmed"):
        raise PolicyError("not_awaiting", f"当前状态是 {st['status']}，没有可拒绝的成片", "")
    if reason not in REASONS:
        raise PolicyError("bad_reason", f"原因码不认识：{reason}", f"可选：{'、'.join(sorted(REASONS))}")
    d = store.job_dir(job_id)
    rej = read_json(d / "rejections.json", {"items": []})
    targets = [x for x in st.get("deliverables", []) if x.get("status") != "rejected" and (not outputs or x["id"] in outputs)]
    if outputs and not targets:
        have = "、".join(x["id"] for x in live(st)) or "（无）"
        raise PolicyError("no_such_output", f"没有这些成片：{outputs}", f"当前可拒绝的成片：{have}")
    zones = [x["range"] for x in targets if x.get("range")]
    rej["items"].append({"t": time.time(), "reason": reason, "text": text[:500], "outputs": [x["id"] for x in targets], "zones": zones})
    atomic_write_json(d / "rejections.json", rej)
    for x in targets:
        x["status"] = "rejected"
    st["status"] = "rejected"
    _metric(store.root, job_id, "rejected", n=len(targets), reason=reason)
    return _save(store, job_id, st, "rejected", reason=reason, outputs=[x["id"] for x in targets])


def rejection_context(store: Store, job_id: str) -> dict[str, Any]:
    """重新选段要带上的反馈：被拒过的区间（不得再选）+ 原因码与用户原话 + 已拒绝轮数。"""
    rej = read_json(store.job_dir(job_id) / "rejections.json", {"items": []})
    return {"zones": [list(z) for it in rej["items"] for z in it.get("zones", [])],
            "feedback": [{"reason": it["reason"], "text": it.get("text", "")} for it in rej["items"]], "rounds": len(rej["items"])}


def _metric(root: Path, job_id: str, event: str, **info: Any) -> None:
    """成片通过/被拒的流水账，止损用（最近 10 条通过率 <50% → 切多备选模式）。"""
    try:
        d = root / "_worker"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "metrics.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "job": job_id, "event": event, **info}, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("写 metrics 失败：%s", e)


def pass_rate(root: Path, last: int = 10) -> dict[str, Any]:
    """最近 last 条已裁决成片的通过率；不足 3 条时不下结论。"""
    p = root / "_worker" / "metrics.jsonl"
    rows = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["event"] in ("confirmed", "rejected"):  # uploaded 等其它流水账不算裁决
                rows += [r["event"]] * int(r.get("n", 1))
    except (OSError, ValueError):
        pass
    rows = rows[-last:]
    ok = sum(1 for x in rows if x == "confirmed")
    rate = ok / len(rows) if rows else None
    return {"n": len(rows), "confirmed": ok, "rate": rate, "degraded_mode": bool(rows) and len(rows) >= 3 and rate is not None and rate < 0.5}


def make_preview(src: Path, dst: Path, *, ffmpeg: str = "ffmpeg", max_bytes: int = PREVIEW_MAX_BYTES) -> dict[str, Any]:
    """压一个 ≤max_bytes 的确认用预览（微信文件上限 25 MiB）。成片本身不动。"""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)], capture_output=True, text=True, check=True).stdout.strip())
    audio = 40_000  # 单声道 40k：预览只用来核对剪得对不对
    vbr = int(max_bytes * 8 * 0.9 / max(dur, 1.0)) - audio
    if vbr < 40_000:
        raise PolicyError("preview_too_long", f"成片 {dur:.0f}s 太长，压不进 {max_bytes / 2**20:.0f} MiB 预览", "改发片段预览或截图，并告诉用户完整成片在 mini 上")
    short_side = 720 if vbr >= 1_000_000 else 540 if vbr >= 500_000 else 360 if vbr >= 90_000 else 288
    # 横屏限高、竖屏限宽，保持比例，偶数边长
    vf = f"scale='if(gt(iw,ih),-2,min({short_side},iw))':'if(gt(iw,ih),min({short_side},ih),-2)'"
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vbr),
           "-maxrate", str(int(vbr * 1.4)), "-bufsize", str(vbr * 2), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "40k", "-ac", "1", "-movflags", "+faststart", str(dst)]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
    size = dst.stat().st_size
    log.info("预览：%s → %s %.1fMiB v=%dk %.1fs", src.name, dst.name, size / 2**20, vbr // 1000, time.perf_counter() - t0)
    if size > max_bytes + 2 * 2**20:
        raise PolicyError("preview_too_big", f"预览 {size / 2**20:.1f} MiB 超限", "降低码率重试")
    return {"path": str(dst), "bytes": size, "video_kbps": vbr // 1000}


def handoff(store: Store, job_id: str, to: str, title: str, desc: str, root: Path, *, kaorou: dict[str, Any] | None = None) -> dict[str, Any]:
    st = state(store, job_id)
    if st["status"] not in ("confirmed", "handed_off"):
        raise PolicyError("not_confirmed", f"成片还没有用户确认（状态：{st['status']}），不能交付或上传", "先把预览发给用户，拿到明确同意后 confirm --quote '用户原话'")
    if to not in ("local", "kaorou"):
        raise PolicyError("handoff_target_unsupported", f"不支持的交付目标：{to}", "local（落盘到 <root>/handoff/）| kaorou（交烤肉上传，默认仅自己可见）")
    d = store.job_dir(job_id)
    dest = root / "handoff" / job_id
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for dv in live(st):
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
    result: dict[str, Any] = {"ok": True, "to": to, "dir": str(dest), "files": files, "credits": credits}
    if to == "kaorou":
        from autocut.worker import kaorou as K  # noqa: PLC0415

        k = kaorou or {}
        prov = read_json(d / "provenance.json", {"sources": []})
        src_url = next((x.get("url") for x in prov["sources"] if x.get("url")), "") or ""
        third = bool((store.get_job(job_id).get("config") or {}).get("third_party")) or bool(src_url)
        mains = [dv for dv in live(st) if dv.get("role", "main") == "main"]
        ups = []
        for dv in mains:
            f = dest / Path(dv["final"]).name
            sha = K._sha256(f)
            req = K.build_request(f, title=(title if len(mains) == 1 else f"{title}｜{dv.get('title', '')}").strip("｜"), desc=desc, tags=list(k.get("tags") or []), tid=int(k.get("tid") or K.DEFAULT_TID),
                                  visibility=str(k.get("visibility") or "self"), source_url=src_url, third_party=third, credits=credits, season=k.get("season"), client_id=K.client_id_for(job_id, f, sha))
            sess = K.upload(f, req, str(k.get("api") or K.API))
            ups.append({"deliverable": dv["id"], "upload_id": sess.get("upload_id"), "job_id": sess.get("job_id"), "status": sess.get("status"), "visibility": req["visibility"]})
        result["kaorou"] = ups
        _metric(root, job_id, "uploaded", n=len(ups))
    st["status"] = "handed_off"
    _save(store, job_id, st, "handed_off", to=to, files=files, **({"kaorou": result.get("kaorou")} if to == "kaorou" else {}))
    return result
