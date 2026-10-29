# 架构与模块契约（实现者必读）

日期：2026-10-28 · 依据：`docs/design/autocut/03-ideate/concepts.md`（概念 A「方案即文档」）、`04-prototype/wireframes.md`

## 数据流

```
素材 ──▶ Media（analysis.py：探测 + 信号 + 磁盘缓存）
          │  speech / words / beats / cuts / loudness / chat
          ▼
       配方 recipes/*.py ──▶ Plan（plan.py，JSON 文件 *.autocut.json）
                                │      ▲
                       审阅页 review/ ──┘（人：切换剪/留、拖边界、选候选）
                       agent  CLI ─────┘（读摘要、改方案）
                                │
             render.py（出片）· export.py（FCPXML/xmeml/EDL/SRT/标记）· check.py（发布前体检）
```

## 模块归属（并行开发时每人只改自己的文件）

| 模块 | 文件 | 负责 |
|---|---|---|
| 方案模型 | `plan.py` | 主线（契约，改动先沟通） |
| 分析门面 | `analysis.py` | 主线 |
| 信号 | `probe/audio/speech/beats/scenes/loudness/chat.py` | 已完成 |
| 语音识别 | `transcribe.py` | recipe-cut |
| 剪辑配方 | `recipes/cut.py`（口播、录课、游戏实况） | recipe-cut |
| 创意配方 | `recipes/highlights.py`、`recipes/montage.py`、`recipes/ytpmv.py`、`recipes/narrate.py` | recipes-creative |
| 出片/导出/体检/字幕 | `render.py`、`export.py`、`check.py`、`captions.py` | render-export |
| 审阅页 | `review/server.py`、`review/static/*` | review-ui |
| CLI 与 agent 接口 | `cli.py`、`skills/`、`AGENTS.md` | 主线（ACI 流程） |

## 契约

### Plan（`src/autocut/plan.py`）
直接读源码，docstring 就是规范。要点：
- `Plan.sources: dict[id, Source]`；`Plan.outputs: list[Output]`；`Output.segments` 按列表顺序拼接 `action == "keep"` 的片段。
- **剪辑类配方**（cut、highlights 的每一条）：segments 覆盖素材的连续时间段，保留和剪掉交替出现，相邻片段首尾相接。这样审阅页才能一键改回保留，`move_boundary` 也能同时挪动邻居。
- **组装类配方**（montage、ytpmv、narrate）：segments 只放保留的片段，按输出顺序排列，用 `slot` 指定在输出里占多长（卡点）。
- 每个片段都要有 `reason`（取值见 `REASONS`）、`confidence`（0–1，低于 0.75 算拿不准）、`detail`（给人看的一句话，中文，例如"停顿 2.6s → 保留 0.4s"）。有文字稿时 `text` 写这段说了什么。`evidence` 放判断用到的数值。
- 片段 id 在一个 output 内唯一，用 `new_segment_id(prefix, n)` 生成，例如 `d001`。
- `Plan.notes`：降级或警告要写在这里，例如"没有找到语音识别模型，只剪了停顿"。**不要静默降级。**
- 改动走 `set_action` / `move_boundary`，保存走 `save()`（原子写，revision +1）。

### 逐词文字稿（`transcribe.py` 产出，`Media.words()` 返回，缓存为 JSON）
```json
[{"text": "大家好", "start": 0.12, "end": 0.86, "p": 0.98}, {"text": "，", "start": 0.86, "end": 0.86, "p": 0.97}]
```
- 时间是**素材时间**（秒）。中文按 whisper token（单字或词），英文合并成整词（以空格开头的 token 开始一个新词，`text` 不带前导空格）。标点单独成项，`start == end`。
- 没有可用模型时，`Media.words()` 返回 `None`。

### Media（`src/autocut/analysis.py`）
`Media(path)` → `.info`（probe 结果）、`.voice_track()`（按音轨标题猜人声轨）、`.audio(track, sr)`、`.speech(track, **kw)`、`.beats()`、`.cuts()`、`.words(language, track)`。结果有磁盘缓存（`AUTOCUT_CACHE` 或素材旁边的 `.autocut-cache/`）。

### 配方的统一签名
```python
def build(sources: list[Path], *, out: Path | None = None, **options) -> Plan
```
返回的 Plan 已经设置好 `path`（默认 `<第一个素材>.autocut.json`），**由调用方决定是否 `save()`**。配方只做判断，不渲染、不打印（用 logging）。

## 约定
- Python 3.14，numpy + ffmpeg 9 + 本机 whisper.cpp；**计费网络：不装新包、不下载任何东西**。用 `source .venv/bin/activate`。
- 类型注解齐全；结果对象用 dataclass；`log = logging.getLogger(__name__)`，关键步骤打日志（参数、耗时、数量）；**注释和给人看的文字用中文**，标识符用英文；不用 print。
- 测试：pytest，`@pytest.mark.unit`（合成数据）/ `@pytest.mark.integration`（用 `fixtures/media` 和它旁边的 `*.truth.json`）。素材缺失时 skip。可以复用 `evals/verify.py` 里的互相关存活检测来判断剪辑结果。
- 错误消息要说清楚：发生了什么、哪个文件或字段、怎么修。
- ffmpeg 子进程：`-nostdin -hide_banner -loglevel error`，长滤镜图用 `-filter_complex_script`（或 ffmpeg 9 的 `-/filter_complex <file>`）。
