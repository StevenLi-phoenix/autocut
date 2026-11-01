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
| 零 LLM | worker 代码里没有网关/模型/key；LLM 全在 DSH | `test_worker_has_no_llm_calls` |
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
| `POST /v1/jobs` `{name, config}` | 开 job。config：`purpose`、`third_party`、`notify`（false 不发事件）、`attribution: {repost_label, text}` |
| `GET /v1/jobs/<job>` | 概况：来源、文件、最近 run、确认状态 |
| `POST /v1/jobs/<job>/ingest` `{url}` 或 `{inbox}` | 下载（复用烤肉的 yt-dlp/PO-Token/cookies，只摘白名单环境变量）；下载后校验音视频流完整性，优先 H.264 |
| `PUT /v1/jobs/<job>/upload?name=x.mp4` | 流式上传素材 |
| `POST /v1/jobs/<job>/run` `{cmd, args}` | 白名单子命令（异步） |
| `POST /v1/jobs/<job>/pipelines/<name>` `{opts}` | 流水线：`cut` `commentary_prepare/_finish` `montage_prepare/_finish` `clips_review` `clips_deliver`（异步） |
| `POST /v1/jobs/<job>/preview` `{max_mib}` | 重新压更小的预览（微信上传超时时用） |
| `GET /v1/jobs/<job>/runs/<run>?wait=N` | 状态/进度/结果（长轮询最多 120 秒） |
| `POST /v1/jobs/<job>/runs/<run>/cancel` | 取消 |
| `GET /v1/jobs/<job>/file?path=` | 下载文件（支持 Range） |
| `POST /v1/jobs/<job>/{confirm,reject,handoff}` | 成片确认、被拒（原因码 + 原话 + 成片 id）、交付（`local` 或 `kaorou`） |
| `GET /v1/metrics` | 最近 10 条成片通过率与是否进入多备选模式 |

## 谁做什么（"一个大脑"，autocut 里零 LLM）
DSH 主 agent 是唯一的大脑：剪哪里、写解说稿、选段、挑镜头、编排 EDL 全部在 DSH 里；worker 只做确定性的"手"（下载、识别、取帧、TTS、混音、校验、渲染、预览、确认门、交付）和"眼"（关键帧拼图、场景切换点、响度曲线、说话段、OCR——复用烤肉的 `vision_ocr.swift`）。**worker 里没有任何 LLM 调用、网关 key 或预算器**（`tests/test_worker_signals.py::test_worker_has_no_llm_calls` 守着）。DSH 的模型 `deepseek-v4.1-flash`（opencode-go）的输入模态含 image，DSH 有 `read_image` 工具，所以 brief 里放拼图路径，agent 自己看（`ar fetch` + `read_image`，或 `ar frame` 取单帧）。agent 的输出一律当不可信输入校验，不合格返回逐条违例和修法。

## 流水线（brief → agent 决定 → finish）
- `cut_prepare` → `cut_finish`：prepare = `autocut cut` 出**提案**（每个停顿/重说/口头禅一个候选：理由、把握度、前后文字）；agent 决定 `keep=[…]`（恢复）和 `cut=[…]`（额外剪）；规则校验：id 存在、不剪锁定片段、"停在句子中间"的剪法自动改回保留并报告、最多剪掉 70%。
- `commentary_prepare` → `commentary_finish`：prepare = cut + 关键帧拼图（带编号和时间）+ 场景切换 + 原声说话段 + OCR；finish = 校验稿子 → TTS（肥鱼，成批串行、按内容缓存，laptop 不通回落 macOS say 并在摘要里标降级）→ 按槽位定速，放不下的句子原样退回 agent 缩短（`lines_too_long`）→ 配音轨 → 方案（`Bed.duck_main_db` 压低原声 + 字幕烧录）→ 体检 → render → 预览。
- `montage_prepare` → `montage_finish`：prepare = `autocut montage` 给拍点槽位 + 镜头中间帧拼图（标签 = 镜头 id）+ ASR 片段；finish = 校验 picks（每个镜头最多一次）→ 替换槽位画面（节拍不变）→ render → 预览。
- clips：agent 用 `transcript`（分页、句段 ID）和 `clip`（按 ID 建候选，硬约束：时长/不切断话/开头有话/不是半句/不悬空开头/不重叠/不在禁区）选段 → `clips_deliver`（敏感词命中拒绝；render 核验 + 确定性 QA：黑屏/冻结；失败用备选顶上）。被拒后 `ar status` 的 `rejections` 带着被拒区间（`clip --forbid`）和原因码，只补被拒的条数，最多 3 轮；最近 10 条通过率 <50% 自动多备选。
- `edl_finish`：agent 写 EDL（`segments:[{source,start,end,speed,note}]` + 可选 `lines` 解说），worker 校验（素材在 job 内或 inbox 链接、时间在素材时长内、倍速 0.25–4（普通变速）或 8–240（延时摄影，4–8 之间不收）、≤300 段、成片 ≤ `max_seconds`）后执行；≥8 倍速的段用"只解关键帧"的延时摄影生成中间片段（几十小时的 HEVC 解不起）。大文件用 `ingest {"inbox_link": …}` 符号链接进 job（只读，分析缓存写到 `<root>/_worker/cache`）。
- 其它：`POST /v1/jobs/<job>/frame {source,t,width}` 取单帧。
所有流水线在"等 agent"或"等用户确认"时结束，并向 DSH 发 `autocut.needs_agent` / `autocut.ready`（失败 `autocut.failed`）事件（事件发射器默认可关：secrets 里不放 token 就不发）。

## 部署（mini）
mini 上 worker 是 AMS 服务 `autocut-worker`（2026-11-01 起，以 `dsh-runtime` 身份运行，声明见 `deploy/ams/service.toml`）。更新：在 `~/services/autocut` 里 `git pull` 后跑 `deploy/ams-deploy.sh`（rsync 到部署副本 `/Users/Shared/ams-apps/autocut`、用 `/Users/Shared/ams-apps/python` 的 3.14 建 venv 并非可编辑安装、`ams ctl restart autocut-worker`）；`bin/vision_ocr` 随部署副本，ingest 的 yt-dlp 设置在副本的 `ingest.env`（不含密钥：cookies 文件路径、PO-Token 地址）。密钥是 AMS secret `AUTOCUT_FEIYU_KEY`、`AUTOCUT_DSH_EVENT_TOKEN`（`ams secret set autocut-worker <NAME>`，值走 stdin），`load_secrets` 先读环境变量，再用 `AUTOCUT_WORKER_SECRETS_FILE` 补缺；AMS 下 stderr 格式是 `LEVEL name: message`，WARNING 及以上进 AMS escalation。旧的 LaunchAgent 路径（`deploy/install-mini.sh`、`deploy/app.lishuyu.autocut.worker.plist`、tcc-host、`worker.env`）只作回滚保留。**重启 worker 会中断进行中的流水线**（启动时标为 failed）：重启前先看 `GET /v1/jobs` 没有 running。
