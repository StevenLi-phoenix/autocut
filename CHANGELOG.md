# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

### Added
- `deploy/ams-deploy.sh` 也能以 `dsh-runtime` 身份运行：已经是服务用户时不再走 sudo，DSH 可以自己部署。
- `autocut-worker`：本机 HTTP 外壳（白名单、路径限定、异步 run、取消、重活互斥、ingest、上传/下载），供 DSH agent 调用。
- 流水线 `cut_prepare`/`_finish`、`commentary_prepare`/`_finish`、`montage_prepare`/`_finish`、`clips_deliver`、`edl_finish`：worker 做手和眼，判断（写稿、选段、挑镜头）由 DSH agent 做；成片确认门（confirm/reject/handoff），handoff 到烤肉（默认仅自己可见）。
- CLI `transcript`（分页、句段 ID）与 `clip`（按 ID 新建候选 + 硬约束）；`Bed.duck_main_db`（解说压低原声）。
- DSH 事件 `autocut.needs_agent` / `autocut.ready` / `autocut.failed`；TTS（肥鱼 → macOS say）回落与缓存；确定性的"眼睛"（关键帧拼图、响度曲线、OCR）、`edl_finish`（agent 写剪辑计划、worker 校验执行，含延时摄影）、inbox 符号链接、取帧接口。
- **autocut 里没有任何 LLM 调用**（用户 2026-10-31 的决定）：判断全部由 DSH agent 做。
- worker `POST /v1/jobs/<job>/sheet`：一批时间点拼成一张带编号和时间的图，agent 一次看多处。

### Changed
- worker 迁入 AMS（服务 `autocut-worker`，以 `dsh-runtime` 运行）：`load_secrets` 先读环境变量 `AUTOCUT_FEIYU_KEY`/`AUTOCUT_DSH_EVENT_TOKEN`/`AUTOCUT_DSH_EVENT_URL`（AMS secret），secrets 文件只补缺；设了 `AMS_SERVICE_ID` 时日志格式为 `LEVEL name: message`；新增 `deploy/ams/service.toml` 与 `deploy/ams-deploy.sh`。
- ingest 不再把 `KAOROU_YT_COOKIES_BROWSER` 传给 yt-dlp（那条路读 Chrome 钥匙串，`dsh-runtime` 没有钥匙串），cookies 只走 `KAOROU_YT_COOKIES_PATH`。

### Fixed
- `fetch` 读不了 inbox 符号链接文件（README/报告）；`edl_finish` 提交时就拒绝 4–8 倍速（方案只收 ≤4，延时摄影从 8 起），不再等到渲染才报错。

### Changed
- ingest 优先 H.264，并校验下载文件的音视频流完整性（AV1 mp4 合并会产出残缺文件）。

## [0.1.0] - 2026-10-28
- 初版：cut / highlights / montage / ytpmv / narrate / transcribe / show / set / review / render / export / check / doctor。
