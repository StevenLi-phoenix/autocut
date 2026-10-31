# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

### Added
- `autocut-worker`：本机 HTTP 外壳（白名单、路径限定、异步 run、取消、重活互斥、ingest、上传/下载），供 DSH agent 调用。
- 流水线 `cut`、`commentary_prepare`/`_finish`、`montage_prepare`/`_finish`、`clips_review`/`clips_deliver`：worker 做手和眼，判断（写稿、选段、挑镜头）由 DSH agent 做；成片确认门（confirm/reject/handoff），handoff 到烤肉（默认仅自己可见）。
- CLI `transcript`（分页、句段 ID）与 `clip`（按 ID 新建候选 + 硬约束）；`Bed.duck_main_db`（解说压低原声）。
- DSH 事件 `autocut.needs_agent` / `autocut.ready` / `autocut.failed`；LLM 网关客户端（只给看图和独立评审用，原始响应先落盘，不自建预算器）；TTS（肥鱼 → macOS say）回落与缓存。

### Changed
- ingest 优先 H.264，并校验下载文件的音视频流完整性（AV1 mp4 合并会产出残缺文件）。

## [0.1.0] - 2026-10-28
- 初版：cut / highlights / montage / ytpmv / narrate / transcribe / show / set / review / render / export / check / doctor。
