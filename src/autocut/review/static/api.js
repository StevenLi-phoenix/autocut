// 和 server.py 的接口一一对应。所有失败都变成 ApiError，带上服务端给的中文说明。

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.error || `请求失败（${status}）`);
    this.status = status;
    this.body = body ?? {};
    this.code = body?.code;
  }
}

async function request(method, url, body, { keepalive = false } = {}) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      keepalive,
    });
  } catch (e) {
    throw new ApiError(0, { error: "连不上审阅页的本地服务。它可能已经被关掉了：回到终端重新运行 `autocut review`，方案不会丢（改动已自动保存）。", code: "offline" });
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* 非 JSON 响应 */ }
  if (!res.ok) throw new ApiError(res.status, data ?? { error: text || res.statusText });
  return data;
}

export const api = {
  plan: () => request("GET", "/api/plan"),
  status: () => request("GET", "/api/plan/status"),
  save: (revision, etag, plan, opts) => request("PUT", "/api/plan", { revision, etag, plan }, opts),
  words: (sid) => request("GET", `/api/words/${encodeURIComponent(sid)}`),
  peaks: (sid, perSec = 50) => request("GET", `/api/peaks/${encodeURIComponent(sid)}?per_sec=${perSec}`),
  render: (outputIds) => request("POST", "/api/render", { output_ids: outputIds }),
  job: (id) => request("GET", `/api/jobs/${id}`),
  cancelJob: (id) => request("POST", `/api/jobs/${id}/cancel`, {}),
  check: (outputId) => request("POST", "/api/check", { output_id: outputId }),
  exportPlan: (format, outputId) => request("POST", "/api/export", { format, output_id: outputId }),
  relocate: (source, path, revision, etag) => request("POST", "/api/relocate", { source, path, revision, etag }),
  reveal: (path) => request("POST", "/api/reveal", { path }),
};
