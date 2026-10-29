# Spec cards：`autocut` CLI（agent 与人共用）

日期：2026-10-28 · 状态：**v1**（已按 `../02-define/affinity.md` 的根因簇修订，见文末“修订记录”）· 依据：`../01-empathize/agent-profiles.md`、`../01-empathize/baseline-observations.md`、人侧线框 `docs/design/autocut/04-prototype/wireframes.md` 第 3 节、`docs/dev/architecture.md`

## 为什么是 CLI + skill（接口形态的取舍，详见 `03-ideate/interface-alternatives.md`）
- 三类 agent 画像都有 shell：P1 是 Claude Code，P2 是它派出的子 agent，P3 是 `claude -p`。CLI 不占工具列表，常驻成本只是一段 skill 描述（启发式 12）。
- 方案是磁盘上的 JSON 文件，人用审阅页、agent 用 CLI，读写的是同一份文件，不需要额外的会话状态。
- baseline 里 agent 已经会用 ffmpeg 处理简单任务，autocut 不应该挡在这条路上，所以要做到"用的时候清楚，不用的时候不打扰"（启发式 11）。
- 参照：Remotion 官方在 2026-10-26 关掉了 MCP，改为 skills 加 md 文档（`docs/design/autocut/01-empathize/reference-remotion.md`）。

## 全局约定（每个命令都遵守，写在 `autocut --help` 的末尾）

| 约定 | 内容 |
|---|---|
| 输出 | stdout 是 TTY 时输出给人看的摘要（带颜色和图标）；不是 TTY，或者加了 `--json`，就输出**一个** JSON 对象，结构稳定。`NO_COLOR` 时不输出颜色 |
| 进度和日志 | 一律写 stderr。默认只写警告；`-v` 写过程，`-vv` 写调试信息；非 TTY 时没有动画 |
| 成功的 JSON | `{"ok": true, "command": "...", ..., "next": [{"cmd": "...", "why": "..."}], "notes": [...]}`。`next` 给出下一步可以直接复制的命令（启发式 6） |
| 失败的 JSON | `{"ok": false, "error": {"code": "...", "message": "发生了什么", "fix": "怎么修，附一个能用的例子", "retry": false}}`，退出码见下表。不打 traceback；需要时用 `--debug` 查看 |
| 退出码 | 0 成功 · 2 用法错误（参数不对）· 3 输入错误（文件不存在、方案不合法、素材读不了）· 4 缺依赖（ffmpeg、语音识别模型、say 的声音）· 5 运行失败（出片失败、超时）· 1 意外错误 |
| 改动 | 所有会改文件的命令都有 `--dry-run`。方案用原子写入，revision 加 1；成片和工程**不覆盖已有文件**，除非加 `--force` |
| 降级 | 不静默降级。缺语音识别模型时照样剪停顿，但 `notes` 里写明"没检测重说/口头禅：……怎么补"，`summary.degraded` 设为 true |
| 时间 | 一律用秒（浮点）；人看的输出另附 `mm:ss` |
| 长度上限 | 任何命令的 JSON 默认不超过约 4k token。列表默认 50 条，带 `total`、`showing`、`next_offset` 和提示 |

## 命令一览

| 命令 | 作用 | 改文件 |
|---|---|---|
| `doctor` | 本机能力自检：ffmpeg、whisper-cli、模型、`say` 的声音；缺什么、怎么补 | 否 |
| `transcribe` | 逐词文字稿（谁在什么时候说了什么）：json / srt / txt，本机离线识别 | 写文字稿文件 |
| `analyze` | 看一眼素材：时长、画面、音轨（猜哪条是人声）、语音占比、BPM | 只写分析缓存 |
| `cut` | 口播、录课、游戏实况：去停顿、去重说、去口头禅 → 方案 | 写方案 |
| `highlights` | 直播录像 → 候选高光（带理由、诱饵识别），默认选前 3 条 → 方案 | 写方案 |
| `montage` | 多段素材 + BGM → 卡点混剪 → 方案 | 写方案 |
| `ytpmv` | 台词素材 + BGM → 鬼畜材料包和第一版对拍 → 方案 | 写方案 |
| `narrate` | 文稿 + 配图 → 配音、字幕、配图的知识视频 → 方案 | 写方案 |
| `show` | 读方案：摘要，或者分页列出片段（可筛选拿不准的） | 否 |
| `set` | 改方案：把片段改成剪或留，挪边界，选或取消候选 | 写方案 |
| `review` | 打开本地审阅页（人用）。非 TTY 时必须加 `--detach`（后台启动，返回 url 和 pid），否则立即失败（`would_block`）：这个命令会一直运行，agent 应当把它交给用户 | 写方案（人改） |
| `render` | 按方案出片 | 写成片 |
| `export` | 导出剪辑软件工程或字幕：fcpxml、xmeml、edl、srt、markers | 写工程 |
| `check` | 发布前体检：留存风险信号，每项附时间点和修法；不是爆款保证 | 可选存档 |

---

## Spec card — `autocut cut`（CLI 命令）

**Description**（`autocut cut --help` 的第一段，agent 读的就是这段）
> 给口播、录课、游戏实况做剪辑方案：剪掉长停顿、重说的段落和口头禅（"嗯""那个"），保留说话。不出片，只写一份方案 `<素材名>.autocut.json`，打印摘要：剪掉多少、按理由分类计数、哪几刀拿不准。
> **适用**：用户说"剪掉停顿/废话/说错的地方""把这条录像剪干净"。**不适用**：只想改个格式、截一段固定时间（直接用 ffmpeg）；直播找高光（用 `highlights`）；配乐卡点（用 `montage`）。
> 有多条音轨时（OBS），自动挑标题像人声的那条来判断；猜不出来就用混音轨，并在 notes 里说明，也可以用 `--track` 指定。有语音识别模型时才能检测重说和口头禅（`autocut doctor` 可以查）。
> 例子：`autocut cut talk.mp4` → `talk.autocut.json`，38:12 → 21:05，剪掉 23 处长停顿、14 处重说、9 处口头禅，3 处拿不准。

**Inputs**
| 参数 | 类型 / 取值 | 必填 | 默认 | 说明 + 例子 |
|---|---|---|---|---|
| `MEDIA...` | 文件路径，可以多个（按顺序接起来） | 是 | — | `talk.mp4` |
| `--pace` | `tight` \| `natural` \| `relaxed` \| 秒数 | 否 | `natural` | 停顿保留多长。讲课用 `relaxed`；`--pace 0.3` 表示每处停顿都留 0.3s |
| `--no-retakes` | 开关 | 否 | 关 | 不检测重说 |
| `--no-fillers` | 开关 | 否 | 关 | 不检测口头禅 |
| `--lang` | `zh` \| `en` \| … | 否 | 自动 | 识别语言 |
| `--track` | 音轨号（1 起）或标题 | 否 | 自动 | 用哪条音轨判断说话 |
| `--keep-action` | `auto` \| `on` \| `off` | 否 | `auto` | 游戏实况里"没人说话但画面或游戏声很激烈"的停顿是否保留；`auto` 表示有第二条音轨或画面变化时启用 |
| `--protect` | `开始-结束`（秒），可以重复 | 否 | — | 这些区间一律保留：`--protect 61.5-75` |
| `-o, --out` | 路径 | 否 | `<素材>.autocut.json` | 方案写到哪里 |
| `--dry-run` | 开关 | 否 | 关 | 只打印摘要，不写方案 |

**Output**（JSON；TTY 时输出线框第 3 节那样的人看摘要）
```json
{"ok": true, "command": "cut", "plan": "talk.autocut.json", "revision": 1,
 "summary": {"duration": 1265.0, "source_span": 2292.0, "removed": 1027.0,
   "cut": {"silence": 23, "retake": 14, "filler": 9},
   "uncertain": [{"id": "d031", "at": 612.4, "action": "cut", "reason": "filler", "confidence": 0.71, "detail": "嗯 · 口头禅", "text": "嗯"}],
   "uncertain_total": 3, "degraded": false},
 "analysis": {"track": "音轨 2（Mic）", "asr": "large-v3-turbo", "seconds": 41.2},
 "notes": [],
 "next": [{"cmd": "autocut show talk.autocut.json --filter uncertain", "why": "逐条看拿不准的 3 处"},
          {"cmd": "autocut review talk.autocut.json", "why": "让人在浏览器里核对（推荐）"},
          {"cmd": "autocut render talk.autocut.json", "why": "直接出片"}]}
```
大小上限：`uncertain` 最多列 10 条，`uncertain_total` 给出总数。

**Errors**
| 情况 | agent 看到的 message / fix | 退出码 |
|---|---|---|
| 素材不存在 | `找不到素材 talk.mp4（当前目录 /x）` / `检查路径；支持 mp4/mov/mkv/wav/m4a…` | 3 |
| 读不了音轨 | `talk.mp4 没有音轨` / `cut 需要有声音的素材；纯画面素材用 montage` | 3 |
| `--track 5` 越界 | `talk.mp4 只有 2 条音轨：1 Mix、2 Mic` / `用 --track 2` | 2 |
| 没有 ffmpeg | `找不到 ffmpeg` / `brew install ffmpeg，然后 autocut doctor` | 4 |
| 没有识别模型 | 不报错：照样剪停顿，notes 写明原因和补法，`degraded: true` | 0 |

**Annotations / safety**：只读素材 · 写方案文件（原子写）· 幂等（同样的输入得到同样的方案）· 不联网 · `--dry-run`

**对应的 storyboard 步骤**：SB1-To-be 第 1–2 格；eval T1–T5

---

## Spec card — `autocut show`

**Description**
> 读方案，不修改。默认打印摘要，和 `cut` 等命令的输出一样。加 `--segments` 分页列出片段；`--filter uncertain` 只看拿不准的，适合在交给人之前先自查。
> 用来回答"剪了什么、为什么剪"。不要直接读整个方案 JSON，几百个片段会占满上下文。
> 例子：`autocut show talk.autocut.json --filter uncertain` → 3 条，每条带时间、动作、理由、把握度和说了什么。

**Inputs**
| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `PLAN` | 路径 | — | 方案文件 |
| `--output` | 输出 id | 第一个 | 多条输出时（高光）选哪条；`--output all` 列出全部输出的摘要 |
| `--segments` | 开关 | 关 | 列片段，不只是摘要 |
| `--filter` | `all` \| `uncertain` \| `cut` \| `keep` \| 理由名（`retake`…） | `all` | 用了 `--filter` 就隐含 `--segments` |
| `--around` | 秒 | — | 只看这个时间点前后 ±10s 的片段 |
| `--limit` / `--offset` | 整数 | 50 / 0 | 分页 |

**Output**
```json
{"ok": true, "command": "show", "plan": "talk.autocut.json", "revision": 3, "output": "main",
 "segments": [{"id": "d031", "start": 612.4, "end": 612.9, "out_at": 301.2, "action": "cut", "reason": "filler",
               "confidence": 0.71, "detail": "嗯 · 口头禅", "text": "嗯", "edited": false}],
 "total": 3, "showing": 3, "next_offset": null}
```
截断时附 `"notice": "只显示了 50/312 条；用 --offset 50 翻页，或者 --filter uncertain 只看拿不准的"`。

**Errors**：方案不存在或不合法 → 3（附 `Plan.load` 的具体原因）；`--output x` 不存在 → 2，并列出可选的 id。

**Annotations**：只读 · 幂等

---

## Spec card — `autocut set`

**Description**
> 修改方案，和审阅页里的操作等价：`--keep`/`--cut` 切换片段的剪或留；`--start`/`--end` 挪边界，首尾相接的邻居会跟着动；`--select`/`--deselect` 选或取消候选输出（高光）。改动会写进方案，revision 加 1，被改的片段标记为人工确认（confidence 1.0）。
> 先用 `show` 找到片段 id，再用 `set`。不要手改 JSON：`set` 会校验不变量（边界不越过邻居、不出素材范围），手改容易把方案改坏。
> 例子：`autocut set talk.autocut.json d031 d044 --keep` → 2 处改为保留，成片 +1.3s。

**Inputs**
| 参数 | 类型 | 说明 |
|---|---|---|
| `PLAN` | 路径 | |
| `ID...` | 片段 id 或输出 id | 可以多个 |
| `--keep` \| `--cut` | 二选一 | 对片段生效 |
| `--start T` \| `--end T` | 秒（素材时间） | 只能对一个片段用 |
| `--select` \| `--deselect` | 开关 | 对输出 id 生效 |
| `--output` | 输出 id | 片段 id 有歧义时用 |
| `--expect-revision N` | 整数 | 方案的 revision 不是 N 就拒绝修改（防止覆盖人在审阅页里的改动）|
| `--dry-run` | 开关 | 只显示会怎么变 |

**Output**：`{"ok": true, "command": "set", "plan": "...", "revision": 4, "changed": [{"id": "d031", "action": "keep", "was": "cut"}], "duration": {"before": 1265.0, "after": 1266.3}}`

**Errors**
| 情况 | message / fix | 退出码 |
|---|---|---|
| id 不存在 | `方案里没有 id 'd99'` / `用 autocut show PLAN --segments 查看；相近的 id：d029、d031` | 3 |
| 边界越界 | `d012 的 end 只能在 40.10–48.70 之间` / 附可用范围 | 2 |
| revision 不匹配 | `方案已经被别人改过（现在是 rev 5，你期望 rev 3）` / `先 autocut show 重新读，再决定改不改` | 3 |

**Annotations**：改方案 · 幂等（重复设置同一个值不改变 revision）· `--dry-run` · 可以撤销（再 set 回去；`evidence.edited` 保留痕迹）

---

## Spec card — `autocut render`

**Description**
> 按方案出片（ffmpeg）。默认输出到方案旁边：`<名>.<输出id>.mp4`；方案开启字幕时同时写 `.srt`。高光会出所有被选中的输出。完成后核验时长和音画同步，结果写进 JSON。
> 出片前最好先让人看过方案（`review`）或者自己 `show --filter uncertain`。只想交给剪辑软件时用 `export`，不要出片。
> 例子：`autocut render talk.autocut.json` → `talk.main.mp4`，21:05，音画漂移 0.00s，-14.1 LUFS。

**Inputs**：`PLAN` · `--output ID`（可以重复）· `--out-dir` · `--burn-captions/--no-burn-captions` · `--loudnorm LUFS|off` · `--force`（覆盖已有文件）· `--dry-run`（只打印 ffmpeg 命令和滤镜图的位置）

**Output**：`{"ok": true, "command": "render", "results": [{"output": "main", "path": "talk.main.mp4", "duration": 1265.03, "expected": 1265.0, "av_drift": 0.0, "srt": "talk.main.srt", "loudness": {"lufs": -14.1}, "verify": {"verified": true, "joints": 46, "joints_mid_speech": 0}, "warnings": []}], "seconds": 38.2, "next": [{"cmd": "autocut check talk.main.mp4", "why": "发布前体检"}]}`

**Errors**：成片已存在 → 3（`加 --force 覆盖，或者 --out-dir`）· ffmpeg 失败 → 5（附 stderr 的最后 20 行和滤镜图文件路径）· 时长和预期差超过 1 帧 → 成功，但写进 warnings

**Annotations**：写新文件，默认不覆盖 · 耗时长（进度写 stderr）· 幂等

---

## Spec card — `autocut export`

**Description**
> 把方案导出给剪辑软件或字幕平台：`fcpxml`（Final Cut、DaVinci Resolve）、`xmeml`（Premiere）、`edl`、`srt`、`markers`（只打标记，不剪，适合想自己剪的人）。被剪掉的段落默认写成标记（`--cuts markers`），在剪辑软件里还看得见；也可以写成禁用片段（`disabled`），或者干脆不写（`none`）。
> 例子：`autocut export talk.autocut.json --format fcpxml` → `talk.main.fcpxml`

**Inputs**：`PLAN` · `--format fcpxml|xmeml|edl|srt|markers`（必填）· `--output ID` · `--cuts markers|disabled|none` · `-o PATH` · `--force`

**Output**：`{"ok": true, "command": "export", "path": "talk.main.fcpxml", "format": "fcpxml", "verified_in_nle": false, "limits": ["叠加层和 ducking 不导出"], "tell_user": "已生成 Final Cut / Resolve 工程，结构已自检，还没在剪辑软件里打开过"}`

**Errors**：格式名不对 → 2（列出可选项）

**Annotations**：写新文件，默认不覆盖 · 幂等

---

## Spec card — `autocut check`

**Description**
> 发布前体检：找出会让观众划走的地方，每一项都给时间点和修法。检查项包括开头多久进正题、死寂、节奏、响度、画幅和平台是否匹配、时长、字幕。对成片（mp4）和方案（json）都能做；对方案做的话不用出片，几秒钟就有结果。
> **这是留存风险信号，不是爆款保证。**向用户转述时要说清楚这一点。
> 例子：`autocut check talk.main.mp4 --platform bilibili` → 2 项风险："0:00–0:12 开头 12 秒才进入正题……"

**Inputs**：`TARGET`（成片或方案）· `--platform auto|douyin|tiktok|shorts|reels|bilibili|youtube` · `--output ID` · `--no-asr`（不跑语音识别，更快）· `--save PATH`（把报告存档成 JSON，发布后用来对照留存曲线）

**Output**：`{"ok": true, "command": "check", "platform": "bilibili", "duration": 1265.0, "items": [{"id": "slow_hook", "severity": "risk", "at": 0.0, "end": 12.0, "message": "...", "fix": "..."}], "summary": "...不是爆款保证。"}`

**Annotations**：只读（`--save` 时写报告）· 第一次可能慢（语音识别，有缓存）

---

## Spec card — `autocut highlights` / `montage` / `ytpmv` / `narrate`

共同点：写方案，打印摘要，`next` 里给出 `review` 和 `render`。错误、退出码、`--dry-run`、`-o` 都和 `cut` 一致。

| 命令 | 何时用（和兄弟命令的区别） | 主要参数 | 摘要里给什么 |
|---|---|---|---|
| `highlights VOD` | 直播或长录像切成几条短视频。理由看弹幕爆发、音量峰、关键词、画面剧变，**并识别抽奖刷屏这类诱饵**。和 `cut` 的区别：`cut` 输出一条剪短的长视频，`highlights` 输出多条独立短片 | `--chat chat.jsonl`、`-n 3`、`--candidates 8`、`--length 15-30`、`--aspect 9:16`、`--reframe center\|track` | 候选清单：每条的时间、分数、理由；被排除的诱饵和排除原因；哪几条被选中 |
| `montage CLIPS... --music M` | 一堆素材配一首歌做卡点混剪 | `--length 15`、`--cut-every auto\|beat\|bar`、`--aspect` | BPM、小节数、镜头数、每个镜头来自哪段素材 |
| `ytpmv CLIP --music M` | 鬼畜：把台词切成音节、对到节拍上。输出是**材料**（音节切片、节拍网格）加第一版对拍，编排要靠人在剪辑软件里做 | `--pattern auto\|beat\|half`、`--pitch` | 音节数（附文字）、BPM、放置次数；建议 `export --format fcpxml` |
| `narrate SCRIPT.md IMAGES...` | 按文稿生成配音、字幕、配图的知识视频 | `--voice`、`--music`、`--size 1920x1080` | 配音时长、句数、每张图停留多久、开头钩子长度；提醒核对图和内容是否对应 |

---

## Spec card — `autocut doctor` / `analyze`

- `doctor`：`{"ok": true, "ffmpeg": {"path": "...", "version": "9.0", "filters": {"subtitles": true, "rubberband": true}}, "asr": {"available": true, "model": "models/ggml-large-v3-turbo.bin", "cli": "/opt/homebrew/bin/whisper-cli"}, "tts": {"say": true, "voices": ["Tingting", "Samantha"]}, "missing": [], "fix": []}`。缺东西时，`fix` 里写具体命令和预估下载量，**不会**自动下载。
  - 这张卡针对的是 baseline 里观察到的行为：agent 会扫整个磁盘找 ASR 模型和工具（U 系列卡片）。
- `analyze MEDIA`：`{"ok": true, "duration": 2292.0, "video": "1920x1080 30fps VFR", "audio": [{"track": 1, "title": "Mix"}, {"track": 2, "title": "Mic", "voice_guess": true}], "speech_ratio": 0.62, "bpm": null, "suggest": [{"cmd": "autocut cut talk.mp4", "why": "62% 的时间在说话，像口播"}]}`
  - 只读，结果写进分析缓存。用于不知道该用哪个配方的时候。

## Spec card — `autocut transcribe`

**Description**
> 本机离线语音识别（whisper.cpp），输出逐词文字稿：谁在什么时候说了什么。格式有 `json`（逐词时间戳）、`srt`、`txt`。需要知道素材里说了什么的时候用它，比如找重说、找高光里的台词、只要字幕；**不要自己在磁盘上找模型或别的项目里的 whisper**。没有模型时会直接告诉你缺什么、怎么补（也可以先跑 `autocut doctor`）。
> 例子：`autocut transcribe talk.mp4 --format srt` → `talk.srt`，412 句，用的模型是 large-v3-turbo。

**Inputs**：`MEDIA` · `--lang` · `--track` · `--format json|srt|txt`（默认 `json`）· `-o PATH` · `--force`
**Output**：`{"ok": true, "command": "transcribe", "path": "talk.words.json", "model": "large-v3-turbo", "words": 5210, "duration": 2292.0, "preview": [{"at": 0.12, "text": "大家好，欢迎回到我的频道。"}], "preview_total": 412}`。`preview` 最多列 20 句。
**Errors**：没有模型或 whisper-cli → 4，`fix` 写明模型应该放在哪里、预估下载量；**不会自动下载**
**Annotations**：只读素材，写文字稿文件，结果有缓存 · 第一次慢（CPU 识别），之后复用缓存

## 修订记录（v0 → v1，依据 Define 的根因簇）

| 根因簇（affinity.md） | 证据 | 改动 |
|---|---|---|
| A-1 / E-2：找不到"谁在什么时候说了什么"的来源，只能扫盘找 ASR | 12/12 个语义任务扫过盘；5/36 执行了别的项目 venv 里的代码 | 新增 `transcribe`；`doctor` 和 skill 写明"需要转写就用它，不要自己找模型"；缺模型时退出码 4，附具体修法，不让 agent 去搜 |
| A-3 / E-1：主观词没有判据，agent 硬凑数量 | T6 haiku 0/2，选了抽奖段；H5 测凑数 | highlights 宁缺毋滥：补位的弱候选只列出、不自动选，notes 写"只找到 k 条（要 n 条）"和手动选的命令；每个候选都附理由 |
| B-1 / B-2：没有独立于生成过程的验证，只能拿必然成立的代理量宣布成功 | 35/36 没听过成片；T9 的"可整除 ≠ 在拍上" | render 出片后**独立核验**：重新测量成片的时长、音画漂移、响度，并在成片上重新检测语音，检查每个接缝是否落在说话中间（`verify.joints_mid_speech`）。核验不过就写进 warnings，同时 `ok` 仍为 true、`verified` 为 false |
| B-3：没在剪辑软件里打开，却声称"能导入" | T4 8/8；D4 | export 的 JSON 带 `"verified_in_nle": false` 和 `tell_user`（给用户转述的一句话）："已生成，结构已自检，还没在剪辑软件里打开过" |
| D-1：决定不落盘，委托人想复核只能整条重看 | haiku 16/18 只留下了成片 | 方案文件本身就是审计记录（每刀都有 reason、confidence、detail）；render 默认把成片写在方案旁边；skill 要求 agent 在最后回复里给出方案路径 |
| C-1 / C-3：原始工具的反馈要靠猜，空输出有四种含义 | B36 | 每个命令都返回显式状态；结果为空时一律附 `notice`，例如"0 处拿不准：全部有把握"，不返回空列表了事 |
| D-3：按模式 pkill | 4 个 transcript | 长命令（render、transcribe）开始时向 stderr 写 `pid=N（取消：kill N）`；收到 SIGTERM 时停掉自己的 ffmpeg 子进程，并删掉写了一半的文件；skill 写明"取消就按 PID kill" |
| 成本守门 | haiku T1/T2 每个 $0.01–0.03、4–16 个调用 | 常见流程控制在 2–3 次调用：`cut` → `render`（→ `check`）；skill 描述 ≤ 120 字 |

### v1 → v1.1（纸面原型，见 `paper-prototype.md` P1–P12）
`tell_user` 替换 `say`；`review` 非 TTY 时必须 `--detach`；配方遇到已有方案时不加 `--force` 就失败（`plan_exists`）；`show --around` 支持 m:ss，默认按成片时间；help 写明 pace 各档秒数、格式和软件的对应、uncertain 阈值 0.75、render 的核验项；取消时删掉写了一半的文件。

## 待验证（纸面原型和 eval 要回答的）
1. P2（haiku）只看 `--help` 和 skill 描述，能不能选对配方、在交给人之前用 `show --filter uncertain` 自查，而不是直接读整个方案 JSON？
2. `next` 字段是在帮忙，还是会诱导 agent 不问用户就直接 render？（委托人要求在出片前能看到剪了什么）
3. should-NOT-use：用户只说"把这个 mp4 转成 720p"或"截 1:00–1:30"时，agent 还会不会用 ffmpeg，而不是硬套 autocut？
