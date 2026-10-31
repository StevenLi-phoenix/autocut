# autocut-worker（接入 DSH 的外壳）

把 autocut CLI 和几条"需要判断"的流水线包成本机 HTTP 服务（`src/autocut/worker/`），给 DSH agent 用。agent 只能：开 job、放素材、发起白名单里的子命令或流水线、取结果/文件、`confirm`/`reject`/`handoff`。

## 边界（为什么这样设计）
| 约束 | 实现 | 测试 |
|---|---|---|
| 只绑回环 | `server.main` 拒绝非回环地址 | — |
| 子命令白名单 | `policy.COMMANDS`（无 `review`、`ytpmv`） | `test_command_whitelist` |
| 路径限定在 `<root>/<job>/` | `policy.inside`：解析符号链接，`..`、绝对路径、`~` 一律拒绝 | `test_paths_outside_job_rejected` 等 |
| 重活互斥 | `Runner` 的 heavy 信号量（默认 1）+ `nice 10`；默认软编 libx264，不碰 VideoToolbox（与烤肉争用） | `test_heavy_jobs_are_serialized_and_queued_cancel` |
| 按 PID 取消 | `Runner.cancel` 向 autocut 进程发 SIGTERM（它自己停 ffmpeg、删半成品），8 秒后强杀进程组 | `test_cancel_running_by_pid` |
| 干净环境 | 子进程只带 PATH（私有 ffmpeg 在前）、HOME、AUTOCUT_ENCODER 等，不透传 worker 的环境 | `test_build_env_is_clean` |
| 确认门 | `delivery.handoff` 只在状态 confirmed 之后可用；confirm 必须附用户原话（写入审计记录） | `test_handoff_blocked_until_confirmed` 等 |
| 预算 | 每个 job 的 LLM 现金上限（默认 ¥2），超了停止调用 | `test_gateway_budget_hard_stop` |
| 原始响应先落盘 | `<job>/work/llm/NNN-<purpose>.json` | `test_gateway_saves_raw_before_parse_and_tracks_spend` |

## 目录与数据
```
<root>/                      默认 /Volumes/MiniSSD/autocut
  models/                    ggml 模型（~/.cache/autocut/models 软链到这里）
  inbox/                     可选：放进来的文件，ingest {"inbox": 名字} 取走
  handoff/<job>/             确认后交付的成片、字幕、credits.txt、meta.json
  _worker/                   tts-cache/、tmp/、metrics.jsonl（通过率流水账）
  <job>/                     job.json、provenance.json（素材来源）、state.json（确认状态）、rejections.json
    src/ work/ out/ .runs/   素材 / 方案与中间产物 / 成片与预览 / run 记录
```

## HTTP API（全部 JSON；失败 `{"ok": false, "error": {code, message, fix}}`）
| 路由 | 作用 |
|---|---|
| `GET /health` | 服务状态、根目录、编码器 |
| `POST /v1/jobs` `{name, config}` | 开 job。config：`purpose`、`third_party`、`budget_cny`、`attribution: {repost_label, text}` |
| `GET /v1/jobs/<job>` | 概况：来源、文件、最近 run、确认状态 |
| `POST /v1/jobs/<job>/ingest` `{url}` 或 `{inbox}` | 下载（复用烤肉的 yt-dlp/PO-Token/cookies，只摘白名单环境变量）；下载后校验音视频流完整性，优先 H.264 |
| `PUT /v1/jobs/<job>/upload?name=x.mp4` | 流式上传素材 |
| `POST /v1/jobs/<job>/run` `{cmd, args}` | 白名单子命令（异步） |
| `POST /v1/jobs/<job>/pipelines/<name>` `{opts}` | 流水线：`cut` `commentary` `montage` `clips`（异步） |
| `GET /v1/jobs/<job>/runs/<run>?wait=N` | 状态/进度/结果（长轮询最多 120 秒） |
| `POST /v1/jobs/<job>/runs/<run>/cancel` | 取消 |
| `GET /v1/jobs/<job>/file?path=` | 下载文件（支持 Range） |
| `POST /v1/jobs/<job>/{confirm,reject,handoff}` | 成片确认、被拒（原因码 + 原话 + 成片 id）、交付（`local` 或 `kaorou`） |
| `GET /v1/metrics` | 最近 10 条成片通过率与是否进入多备选模式 |

## 流水线
- `cut`：cut → 保守策略（拿不准或"停在句子中间"的剪法改回保留）→ 体检 → render → ≤23MiB 预览。
- `commentary`：cut → 抽帧 + 视觉 LLM → 解说稿 LLM → TTS（成批、按内容缓存、失败回落）→ 按槽位定速/缩短/丢弃 → 配音轨 → 方案（`Bed.duck_main_db`）→ 体检 → render → 预览。
- `montage`：`autocut montage` 给拍点槽位 → 镜头库视觉理解（+ASR 片段）→ LLM 按主题挑镜头 → 替换槽位画面（节拍不变）→ render。
- `clips`：转写 → 提名（分块）→ 硬约束（含自动补句/减句）→ 独立评审 → 信号降权 → 宁缺毋滥 → render + 抽帧 QA（失败换备选）→ 预览。被拒后带原因与禁区重选，最多 3 轮。
所有流水线结束时状态为 `awaiting_confirmation`，并向 DSH 发 `autocut.ready` 事件（失败发 `autocut.failed`）。

## 部署（mini）
`deploy/install-mini.sh [--restart]`：拉代码、同步依赖、登记 `~/.config/tcc-host/allow.json`、写 LaunchAgent `app.lishuyu.autocut.worker`（经 tcc-host 拉起，持有外置盘授权）。密钥在 `~/services/autocut/worker.env`（600）：`AUTOCUT_LLM_KEY`（网关专用 key）、`AUTOCUT_DSH_EVENT_TOKEN`；其余配置见 `deploy/app.lishuyu.autocut.worker.plist`。**重启 worker 会中断进行中的流水线**（启动时标为 failed）。
