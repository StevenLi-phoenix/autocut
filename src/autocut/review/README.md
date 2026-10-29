# 审阅页（review）

本地网页：创作者在这里核对自动剪辑方案的每个决定、改掉错的，然后出片或导出工程。设计隐喻是文档的**修订模式**——机器提出修改建议（✂ 剪掉、⚠ 拿不准），人逐条接受或拒绝。人和 agent 改的是同一份方案 JSON。

```bash
scripts/review_demo.sh              # 口播演示（单条时间线）
scripts/review_demo.sh highlights   # 直播高光演示（候选清单）
python -c "from autocut.review.server import serve; serve('talk.autocut.json')"
```

## 服务器（`server.py`，只用标准库，只绑定 127.0.0.1）

| 接口 | 说明 |
|---|---|
| `GET /api/plan` | 方案 + `summary` + 每个素材的元数据和 URL + 各模块是否可用（`capabilities`） |
| `PUT /api/plan` | `{"revision": N, "etag"?: "...", "plan": {...}}`；revision/etag 和磁盘不一致 → **409** 并附磁盘版本（`disk`）；`Plan.from_dict` 校验失败 → **400** 带 PlanError 原文 |
| `GET /api/plan/status` | `{revision, etag}`，页面每 2.5s 轮询，发现 agent/编辑器的外部修改 |
| `GET /media/<source_id>` | 素材文件，支持 Range（206）、HEAD |
| `GET /api/words/<source_id>` | 逐词文字稿（`sources.<id>.words`，否则分析缓存里最新的 `words-*.json`）；没有 → 404 并说明原因 |
| `GET /api/peaks/<source_id>?per_sec=50` | 波形峰值（0–1），经 `Media._cached` 落盘缓存 |
| `POST /api/render` / `GET /api/jobs/<id>` / `POST /api/jobs/<id>/cancel` | 后台线程调 `render.render(plan, output_ids=, progress=)`；取消靠 progress 回调抛 `InterruptedError` |
| `POST /api/check` | `check.check(plan, output_id=)` → `CheckReport.to_dict()` |
| `POST /api/export` | `export.export(plan, fmt, output_id=)`，fmt ∈ fcpxml / xmeml / edl / srt / markers |
| `POST /api/relocate` / `POST /api/reveal` | 重新定位丢失的素材 / 在 Finder 中显示本次生成的文件 |

render / check / export 在请求时才 import；失败返回 **503**（"出片/体检/导出模块不可用"），不影响审阅和保存。安全：Host 必须是 127.0.0.1/localhost（防 DNS rebinding），写请求要 `Content-Type: application/json`（浏览器跨站表单发不出来）且 Origin 同源，静态文件白名单。

## 前端（`static/`，纯 ES modules，无构建）

| 文件 | 职责 |
|---|---|
| `model.js` | 纯逻辑：时间映射、播放列表/跳过列表、`moveBoundary`/`setAction`（和 `plan.py` 一致）、撤销栈、裁切框。`model.test.js` 用 `node --test` 测，边界移动用 `testdata/move_boundary.golden.json` 与 Python 逐例对照（`tests/test_review_server.py` 保证 golden 是最新的）。 |
| `player.js` | 按方案播放：播放列表游标 + rAF 驱动的跳转 |
| `timeline.js` | canvas 波形 + 片段条，缩放、平移、拖边界 |
| `transcript.js` | 文字稿栏（修订标记样式）、筛选 |
| `app.js` | 状态、自动保存（800ms）、冲突处理、快捷键、两种模式、出片/体检/导出 |
| `a11y.test.js` | 从 `style.css` 读设计令牌，校验浅色/深色主题的对比度（文字 ≥ 4.5:1） |

页面离线可用：不引用任何 CDN 或网络字体；字体用系统栈。控制台里 `__autocut` 暴露 state/player/timeline 方便排查。
