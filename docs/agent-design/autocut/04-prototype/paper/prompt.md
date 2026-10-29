你是 Claude Code 里的编程助手，用户让你处理视频。本机装有一个叫 autocut 的命令行工具，下面是你能看到的全部说明（skill 和命令参考）。

===== SKILL.md =====
---
name: autocut
description: 本地自动剪辑视频：去停顿/重说/口头禅、游戏实况去无聊段、直播切高光、卡点混剪、鬼畜对拍、文稿生成知识视频、导出剪辑软件工程、发布前体检、语音转文字稿。用户要"剪""切片""高光""混剪""鬼畜""字幕""导出 FCP/达芬奇/Premiere"时用。只转码、截固定时间段、拼接、压缩时直接用 ffmpeg。
---

# autocut

先生成**方案**（`<素材>.autocut.json`，每一刀都写明理由和把握度），再出片。不要自己写 ffmpeg 滤镜图来判断剪哪里，判断交给 autocut 的配方。

## 三步
1. **出方案**：选一个配方，结果是一份 JSON 摘要（非终端时自动输出 JSON）
   | 用户想要 | 命令 |
   |---|---|
   | 剪停顿 / 说错重来 / "嗯""那个" / 游戏实况没看点的段 | `autocut cut 素材.mp4`（讲课加 `--pace relaxed`） |
   | 直播或长录像切几条短视频 | `autocut highlights 录像.mp4 --chat 弹幕.jsonl -n 3` |
   | 素材加 BGM 卡点 | `autocut montage a.mp4 b.mp4 … --music bgm.mp3` |
   | 鬼畜 | `autocut ytpmv 台词.mp4 --music bgm.mp3` |
   | 文稿做知识视频 | `autocut narrate 文稿.md 图1.png 图2.png …` |
   | 只要文字稿或字幕 | `autocut transcribe 素材.mp4 --format srt` |
2. **核对**：`autocut show 方案.json --filter uncertain`。把拿不准的几处（时间 + 理由）转告用户，并给出 `autocut review 方案.json`（用户在浏览器里逐条核对）。要改就用 `autocut set 方案.json <id> --keep|--cut`，**不要手改 JSON**。
3. **交付**：`autocut render 方案.json`（出片，自带核验），或者 `autocut export 方案.json --format fcpxml|xmeml|edl|srt`。发布前可以跑 `autocut check 成片.mp4`。

## 规则
- 输出里的 `next` 是建议的下一步。用户没说"直接出片"时，render 之前先把拿不准的地方告诉用户。
- 需要转写或语音识别时用 `autocut transcribe`，或者先跑 `autocut doctor` 看本机有什么。**不要在磁盘上搜模型，也不要用别的项目的环境。**缺东西就照 `fix` 告诉用户，不要自己下载。
- `notes` 和 `degraded: true` 必须转告用户，比如"没检测重说：缺语音识别模型"。
- 高光不够数时，autocut 不会凑数。照实告诉用户"只找到 k 条"，不要自己降标准补满。
- export 的工程还没在剪辑软件里打开过，转述时用 JSON 里的 `say` 字段，不要说"能导入"。
- check 报的是留存风险信号，**不是爆款保证**。
- 取消长任务：stderr 里有 `pid=N`，用 `kill N`。**不要用 pkill 或 killall**。
- 最后的回复里写出方案文件路径。它就是"剪了什么、为什么"的记录，以后可以用 `autocut show` 查。
- 退出码：2 参数错，3 输入错，4 缺依赖，5 运行失败。`error.fix` 会写怎么修。

===== autocut 命令参考（--help 摘要）=====
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
|
===== 结束 =====

现在**不要执行任何东西**，只做规划。对下面每个任务，按顺序写出你会运行的确切 shell 命令（不用 autocut 就写你会用什么），说明每一步之后你会看什么、怎么判断成功，以及最后会对用户说什么（一两句）。如果说明里有让你拿不准、读不懂或者觉得命名会误导的地方，在最后单独列一节"困惑之处"。

任务：

T1 "把 talk.mp4 这条口播的长停顿剪掉，别剪太碎"
T2 "lecture.mp4 是教室录的课，没人说话的部分去掉，告诉我剪了多少"
T3 "talk.mp4 里我说错重来的那段和'嗯'都剪掉"
T4 "先别渲染，把 talk.mp4 剪好后导出 FCP / 达芬奇工程，我自己微调"
T5 "game.mp4 是 OBS 多音轨录的游戏实况，没说话又没看点的剪掉，打怪的留着"
T6 "vod.mp4 是直播录像（弹幕在 chat.jsonl），切 3 条高光发抖音，竖屏带字幕"
T7 "clips/ 里 6 段素材，配 music.wav 做个踩鼓点的卡点混剪"
T8 "按 script.md 和 img1-3.png 做个知识视频，要配音、字幕、配图"
T9 "拿 line.mp4 里这句台词配 music.wav 做个鬼畜，字卡在拍子上"
N1 "talk.mp4 多长？什么编码？"
N2 "把 talk.mp4 压到 50MB 以内" 以及 "把 clip1.mp4 转成 GIF"
N3 "把 a.mp4 和 b.mp4 直接首尾拼起来" 以及 "把 v.mp4 顺时针转 90 度"
N4 "把 talk.mp4 的音频提取成 mp3"
N5 （当前目录里有一堆 mp4）"帮我把这个项目的 README 改一下错别字"
N6 "我只要 talk.mp4 的字幕文件，不用剪"
S1 （新会话，没有之前的上下文）"昨天剪的 talk.mp4 那条，3 分 12 秒那段为什么被剪了？帮我恢复"
S3 （你刚启动了一个要跑 10 分钟的出片）"先停一下，别出片了"
H5 "vod2.mp4 切 3 条高光"（假设素材里其实只有 2 段真正精彩的）

输出格式：每个任务一个小节，标题用任务编号。