"""交给烤肉上传（external-uploads API，127.0.0.1:8096，不改烤肉）：创建会话 → 流式 PUT → commit，之后由烤肉的 Upload/Publish 执行。

只在 delivery.handoff 的确认门之后被调用。默认 visibility=self（仅自己可见）：用户确认的是"成片没问题"，
真正公开还要用户自己在 B 站点；明确要求公开时才传 public。第三方内容按转载（copyright=2）投，必须带原链接。
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from autocut.worker.policy import PolicyError

log = logging.getLogger(__name__)
API = "http://127.0.0.1:8096"
DEFAULT_TID = 17  # 单机游戏（二级分区；一级分区会被静默落到 21 生活>日常）


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _req(method: str, url: str, body: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise PolicyError("kaorou_error", f"烤肉返回 {e.code}：{e.read()[:300].decode('utf-8', 'replace')}", "看烤肉 /external-uploads 的文档（docs/external-upload-api.md）；字段不合法就按提示改") from None
    except (urllib.error.URLError, OSError) as e:
        raise PolicyError("kaorou_unreachable", f"连不上烤肉 API：{e}", "告诉用户烤肉没在运行；成片仍在 handoff 目录，之后可重试") from None


def build_request(file: Path, *, title: str, desc: str, tags: list[str], tid: int, visibility: str, source_url: str, third_party: bool, credits: str, season: str | None,
                  client_id: str) -> dict[str, Any]:
    if not title.strip():
        raise PolicyError("title_required", "交给烤肉上传必须有标题", "handoff --title '…'（≤80 字）")
    if not tags:
        raise PolicyError("tags_required", "B 站投稿至少要 1 个标签", "handoff --tags 标签1,标签2（每个 ≤20 字，不含逗号）")
    if visibility not in ("self", "public"):
        raise PolicyError("bad_visibility", f"visibility 只能是 self 或 public，收到 {visibility!r}", "默认 self（仅自己可见）")
    copyright_ = 2 if third_party else 1
    if copyright_ == 2 and not source_url:
        raise PolicyError("source_url_required", "转载内容必须带原链接", "素材来自上传文件时没有原链接——这种情况请用户确认是否自制（third_party=false）")
    description = (desc.strip() + ("\n\n" + credits if credits else "")).strip()[:2000]
    return {"client_upload_id": client_id, "filename": file.name, "title": title.strip()[:80], "description": description, "tags": [t.strip()[:20] for t in tags if t.strip()][:10],
            "size_bytes": file.stat().st_size, "sha256": _sha256(file), "copyright": copyright_, "tid": tid, "visibility": visibility, "season": season, "source_url": source_url[:2000]}


def upload(file: Path, req: dict[str, Any], api: str = API) -> dict[str, Any]:
    """幂等：同一个 client_upload_id + 同样内容重跑会得到同一个会话。"""
    sess = _req("POST", f"{api}/external-uploads", req)
    uid = sess["upload_id"]
    if sess.get("status") == "awaiting_content":
        size = file.stat().st_size
        with file.open("rb") as f:
            r = urllib.request.Request(f"{api}/external-uploads/{uid}/content", data=f, method="PUT", headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
            try:
                with urllib.request.urlopen(r, timeout=3600) as resp:
                    sess = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                raise PolicyError("kaorou_error", f"上传内容被烤肉拒绝 {e.code}：{e.read()[:200].decode('utf-8', 'replace')}", "重试；校验失败说明文件在传输中被改动") from None
    if sess.get("status") in ("ready", "awaiting_content") and not sess.get("job_id"):
        sess = _req("POST", f"{api}/external-uploads/{uid}/commit")
    log.info("交给烤肉：%s → upload=%s job=%s status=%s", file.name, uid, sess.get("job_id"), sess.get("status"))
    return sess


def client_id_for(job_id: str, file: Path, sha: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"autocut:{job_id}:{file.name}:{sha}"))
