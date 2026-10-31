# autocut

本地自动剪辑：配方先写出一份可以审阅的**方案**（`*.autocut.json`，每一刀带理由和把握度），人在审阅页、agent 用 CLI 改同一份方案，再出片、导出剪辑软件工程、发布前体检。全程离线、确定性；LLM 判断（选段、写解说、挑镜头）在 worker 的流水线里，经网关调用，带预算。

- **口播/录课/实况粗剪**（`cut`）：去长停顿、重说、口头禅；无人核对时拿不准的剪法一律改回保留。
- **游戏实况解说配音**（worker 流水线 `commentary`）：抽帧看画面 → LLM 写解说稿 → TTS → 解说压低原声 → 字幕烧录。
- **混剪**（`montage`）：按音乐节拍卡点，镜头由视觉理解 + LLM 按主题挑。
- **长视频切竖屏短片**（`highlights` 信号 / agent 自己选段 `transcript` + `clip`）：多候选 → 硬约束 → 独立评审 → 渲染 QA，宁缺毋滥。
- **接入 DSH**：`autocut-worker` 把 CLI 包成本机 HTTP 服务，DSH agent 在微信里接任务、发成片预览、等用户确认后才交付/上传。详见 [docs/dev/worker.md](docs/dev/worker.md)。

## 用法

```bash
uv venv -p 3.14 && uv pip install -e . --group dev      # macOS arm64，需要 ffmpeg 9（含 libass）和 whisper-cli
autocut doctor                                           # 本机能力自检
autocut cut talk.mp4 && autocut render talk.autocut.json
```

CLI 规格见 [spec-cards](docs/agent-design/autocut/04-prototype/spec-cards.md)，给 agent 的用法见 [skills/autocut/SKILL.md](skills/autocut/SKILL.md)，架构见 [docs/dev/architecture.md](docs/dev/architecture.md)。

## 测试

```bash
python fixtures/make_fixtures.py        # 合成素材 + 真值（不进 git）
python -m pytest -q -m unit             # 秒级单元测试
python -m pytest -q                     # 全量（含集成，需 ffmpeg 与 whisper 模型）
(cd src/autocut/review/static && node --test)
```

## 许可

尚未选定许可证（默认保留所有权利）。第三方素材的版权由使用者按任务自行负责；worker 会为每个任务登记素材来源，并按任务配置决定是否标注转载。
