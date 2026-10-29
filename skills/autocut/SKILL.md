---
name: autocut
description: 本地自动剪辑视频：去停顿/重说/口头禅、游戏实况去无聊段、直播切高光、卡点混剪、鬼畜对拍、文稿生成知识视频、导出剪辑软件工程、发布前体检、语音转文字稿。用户要"剪""切片""高光""混剪""鬼畜""字幕""导出 FCP/达芬奇/Premiere"时用。只查信息、转码、压缩、转 GIF、旋转、拼接、截固定时间段、提取音频时直接用 ffmpeg。
---

# autocut

先生成**方案**（JSON，每一刀写明理由和把握度），再出片。剪哪里由 autocut 的配方判断，不要自己写 ffmpeg 滤镜图来判断。非终端调用时，每个命令都输出一个 JSON；方案路径以返回里的 `plan` 字段为准。

## 三步
1. **出方案**
   | 用户想要 | 命令 | 已经包含 |
   |---|---|---|
   | 剪停顿、说错重来、"嗯""那个" | `autocut cut 素材.mp4` | `--pace` 控制每个停顿保留多长：tight 0.15s、natural 0.4s（默认）、relaxed 0.8s（讲课），也可以直接给秒数 |
   | 游戏实况去掉没说话又没看点的段 | `autocut cut 实况.mp4` | 多音轨时自动挑麦克风轨；没人说话但游戏声或画面激烈的段会保留（打怪）|
   | 直播或长录像切几条短视频 | `autocut highlights 录像.mp4 -n 3 [--chat 弹幕.jsonl]` | 9:16 竖屏、字幕、识别抽奖刷屏；一份方案里有多条，render 时每条出一个文件。弹幕可选，每行 `{"t": 秒, "text": "..."}` |
   | 素材加 BGM 卡点 | `autocut montage a.mp4 b.mp4 … --music bgm.mp3` | 自动测 BPM 和拍点 |
   | 鬼畜 | `autocut ytpmv 台词.mp4 --music bgm.mp3` | 自动转写，按字切成音节，对到拍上；编排建议导出到剪辑软件里做 |
   | 文稿做知识视频 | `autocut narrate 文稿.md 图1.png …` | 配音（macOS 语音）、字幕；图片按顺序分配，文稿里写 `![](图.png)` 可以指定 |
   | 只要文字稿或字幕 | `autocut transcribe 素材.mp4 --format srt` | |
   已有同名方案时，不加 `--force` 不会覆盖（保护人改过的地方）。
2. **核对**：`autocut show 方案.json --filter uncertain`。把握度低于 0.75 的算拿不准。把这几处（时间 + 理由）告诉用户。
   - 如果用户要自己逐条看，**把命令 `autocut review 方案.json` 交给用户自己运行**。它会打开浏览器并一直运行。你要替用户打开就加 `--detach`，它会返回网址和 pid。
   - 要改方案就用 `autocut set 方案.json <id> --keep|--cut`，不要手改 JSON。
   - 用户说的时间（比如"3:12 那段"）默认按成片时间：`autocut show 方案.json --around 3:12`。
3. **交付**：
   - `autocut render 方案.json`：出片。之后会自动核验时长、音画同步，以及接缝是否落在说话中间，结果在 `verify` 里。
   - `autocut export 方案.json --format fcpxml`：给 Final Cut 或达芬奇。`xmeml` 给 Premiere，`edl` 是通用格式，`srt` 是字幕。
   - 发布前可以跑 `autocut check 成片.mp4`。

## 规则
- 用户没说"直接出片"时，render 之前先把拿不准的地方告诉用户。
- 需要语音识别时用 `autocut transcribe`，或者先跑 `autocut doctor` 看本机有什么。**不要在磁盘上搜模型，也不要用别的项目的环境。**缺东西就照返回里的 `fix` 告诉用户，不要自己下载。
- 返回里的 `notes`，以及 `degraded: true`（某项检测没做），都必须转告用户。
- 高光不够数时，autocut 不会凑数。照实说"只找到 k 条"，不要自己降低标准补满。
- export 的工程还没在剪辑软件里打开过，转述时用返回里的 `tell_user`，不要说"能导入"。
- check 报的是留存风险信号，**不是爆款保证**。
- 取消长任务：stderr 里有 `pid=N`，用 `kill N`。半成品会被清掉。**不要用 pkill 或 killall**。
- 最后的回复里写出方案文件路径。它就是"剪了什么、为什么"的记录，以后用 `autocut show` 可以查。
- 退出码：2 参数错，3 输入错（包括会覆盖已有文件），4 缺依赖，5 运行失败或被取消。`error.fix` 会写怎么修。
