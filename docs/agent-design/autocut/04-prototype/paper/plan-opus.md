# 规划：autocut 各任务的执行步骤

**通用前提**：Claude Code 里跑 Bash 时 stdout 不是 TTY，autocut 会自动输出 JSON。我仍然显式加 `--json`，免得依赖自动判断。每一步我都看三样：退出码、`ok`、`notes` 和 `summary.degraded`。失败时按 `error.fix` 处理。

命令参考在"命令一览"那里被截断了，我看不到各命令的具体参数。所以凡是下面标了"先查 help"的步骤，第一条命令都是 `autocut <cmd> --help`，用来确认参数名。

---

## T1

```sh
autocut cut --help                      # 确认 --pace 有哪些档位、有没有最短片段/最小停顿之类的参数
autocut cut talk.mp4 --pace relaxed --json   # 前提是 help 确认 relaxed 就是"少剪、不碎"的意思
autocut show talk.autocut.json --filter uncertain --json
```

- **看什么**：退出码为 0、`ok: true`，记下 `summary` 里的刀数和总共剪掉的时长。如果刀数和时长相比显得很密，说明"太碎"，就换更宽松的档位重跑。
- **出片**：用户没说"直接出片"，所以先不 render。等用户确认后再跑 `autocut render talk.autocut.json --json`，然后看 render 自带的核验结果。
- **对用户说**：「方案出来了：剪掉 N 处长停顿，共 X 秒，用的是宽松节奏。有 k 处拿不准，分别在 mm:ss（理由……）。你可以用 `autocut review talk.autocut.json` 在浏览器里逐条核对，没问题我就出片。方案文件：`talk.autocut.json`。」

## T2

```sh
autocut cut lecture.mp4 --pace relaxed --json
autocut show lecture.autocut.json --filter uncertain --json
```

- **看什么**：从 `summary` 里取剪掉的时长、原时长、剪后时长。教室录音底噪大，静音可能判断不准，所以重点看 uncertain 的条目。缺语音识别模型时 `degraded` 会是 true，这时要把 `notes` 原文转告用户。
- **对用户说**：「原片 mm:ss，去掉 N 段没人说话的部分，共 X 分钟，剪完 mm:ss。其中 k 处拿不准（列出时间和理由），可以用 `autocut review lecture.autocut.json` 核对。」如果 `degraded` 为 true，附上 notes 原文。方案文件路径也一并给出。

## T3

```sh
autocut doctor --json                   # 检测重说和口头禅都要靠语音识别，先确认模型在不在
autocut cut talk.mp4 --json
autocut show talk.autocut.json --filter uncertain --json
```

- **看什么**：如果 doctor 报缺语音识别模型，或者 cut 返回退出码 4 / `degraded: true`，就停下来，把 `fix` 转告用户。我不自己下载，也不去磁盘上找模型。成功的话，确认 summary 里分别有"重说"和"口头禅"两类刀数。
- **注意**：T1 产生的 `talk.autocut.json` 可能已经存在，重跑 cut 会不会冲掉之前用 `set` 做过的修改，我不确定。所以先跑 `autocut cut talk.mp4 --dry-run --json` 看它打算怎么处理已有方案（见困惑之处第 7 条）。
- **对用户说**：「剪掉 N 处说错重来、M 个'嗯'，共 X 秒。拿不准的有……，核对请用 `autocut review talk.autocut.json`。」

## T4

```sh
autocut cut talk.mp4 --json
autocut show talk.autocut.json --filter uncertain --json
autocut export --help                   # 确认 fcpxml 的版本，以及哪种格式适合达芬奇
autocut export talk.autocut.json --format fcpxml --json   # 给 FCP；达芬奇也能导入 FCPXML
autocut export talk.autocut.json --format xmeml --json    # 备选：达芬奇导入 FCPXML 失败时用
```

- **看什么**：export 返回的输出路径和 `say` 字段。工程文件已存在时不加 `--force`，改成提示用户。全程不跑 render。
- **对用户说**：转述时用 `say` 字段的原话，比如「已生成 `talk.fcpxml`（尚未在剪辑软件中打开验证）」，不说"能导入"。同时说明拿不准的几处在哪里，给出方案文件路径。

## T5

```sh
ffprobe -v error -show_entries stream=index,codec_type,codec_name,channels:stream_tags=title,handler_name -of json game.mp4
autocut cut --help                      # 找有没有选择音轨（麦克风轨）或游戏模式的参数
autocut cut game.mp4 [--<音轨参数> N] --json
autocut show game.autocut.json --filter uncertain --json
```

- **看什么**：先用 ffprobe 确认有几条音轨，OBS 通常的分法是游戏声、麦克风、混音。如果 help 里没有选轨参数，cut 默认用哪条轨就不明确，要在回复里说明。还要检查剪掉的段落里有没有游戏音量高的部分（比如打怪），重点看 uncertain 里的理由。
- **对用户说**：「按麦克风轨（第 N 轨）判断有没有说话，同时参考游戏声判断有没有看点，剪掉 N 段，共 X 分钟。拿不准的有……，请用 `autocut review game.autocut.json` 确认打怪的段落都保留了。」

## T6

```sh
autocut highlights --help               # 看 chat.jsonl 的格式要求，以及有没有竖屏/字幕参数
autocut render --help                   # 竖屏 9:16、烧字幕可能是 render 的参数
autocut highlights vod.mp4 --chat chat.jsonl -n 3 --json
autocut show vod.autocut.json --filter uncertain --json
# 用户确认后：
autocut render vod.autocut.json [--竖屏参数] [--字幕参数] --json
autocut check <每条成片>.mp4 --json
```

- **看什么**：如果返回退出码 3，说明 chat 格式不对，按 `fix` 处理。看实际找到几条，不够 3 条就照实说。render 之后确认生成了 3 个文件、分辨率是竖屏、字幕已烧录（用 ffprobe 看 width 和 height）。
- **对用户说**：「找到 3 条高光（时间段 + 理由），已出竖屏带字幕的片子：路径……。发布前体检的留存风险提示如下……（这只是风险信号，不是爆款保证）。方案文件：`vod.autocut.json`。」

## T7

```sh
ls clips/                               # 确认是 6 段，扩展名可能是 .mov/.mp4，文件名顺序就是传入顺序
autocut montage clips/*.mp4 --music music.wav --json
autocut show <方案>.autocut.json --filter uncertain --json   # 方案文件名要从 JSON 输出里取
# 用户确认后：
autocut render <方案>.autocut.json --json
```

- **看什么**：方案文件路径以 JSON 输出为准，因为有多个输入时 `<素材>` 指哪个并不明确。检测到的 BPM 和节拍数要合理，6 段素材都要用上。
- **对用户说**：「按 music.wav（约 N BPM）踩点，6 段素材共切成 M 个镜头，成片路径是……。拿不准的节拍有……。」

## T8

```sh
autocut doctor --json                   # 配音依赖 say 的声音
autocut narrate --help                  # 图片怎么对应文稿段落；字幕是不是默认就有
autocut narrate script.md img1.png img2.png img3.png --json
autocut show <方案>.autocut.json --filter uncertain --json
# 用户确认后：
autocut render <方案>.autocut.json --json
```

- **看什么**：缺声音时会返回退出码 4，把 `fix` 转告用户。确认 3 张图都分配到了文稿段落，字幕已开启。
- **对用户说**：「已按文稿生成配音、字幕和配图，时长 mm:ss，用的声音是……，成片路径……。图片和段落的对应关系是……，不对的话可以调整。」

## T9

```sh
autocut ytpmv --help                    # 怎么指定"哪一句"；字卡是不是参数
autocut transcribe line.mp4 --format srt --json   # 如果 line.mp4 里不止一句，先看有哪几句
autocut ytpmv line.mp4 --music music.wav [--字卡参数] --json
autocut show <方案>.autocut.json --filter uncertain --json
# 用户确认后：
autocut render <方案>.autocut.json --json
```

- **看什么**：如果素材里有多句台词，"这句"指哪一句不明确，要问用户，或者先列出转写结果让用户选。还要确认字卡确实对齐到了拍子上。
- **对用户说**：「用『……』这句台词，按 music.wav 的节拍做了鬼畜，字卡落在拍子上，成片路径……。」

## N1

这类问题不需要 autocut，直接用 ffprobe。

```sh
ffprobe -v error -show_entries format=duration,bit_rate:stream=codec_type,codec_name,profile,width,height,r_frame_rate,sample_rate,channels -of json talk.mp4
```

- **对用户说**：「talk.mp4 时长 mm:ss，视频是 H.264 High、1920×1080、30fps，音频是 AAC 48kHz 立体声。」

## N2

**压缩到 50MB**：先查时长，再算出目标码率，用两遍编码。

```sh
ffprobe -v error -show_entries format=duration -of csv=p=0 talk.mp4
# 视频码率 ≈ 50×8×1024 kbit × 0.95 / 时长 − 128 kbit/s（音频）
ffmpeg -n -i talk.mp4 -c:v libx264 -b:v <N>k -pass 1 -an -f null /dev/null
ffmpeg -n -i talk.mp4 -c:v libx264 -b:v <N>k -pass 2 -c:a aac -b:a 128k talk_50mb.mp4
stat -f %z talk_50mb.mp4
```

- **怎么判断成功**：文件小于 52428800 字节。超了就把码率下调 5% 再压一次。`-n` 保证不覆盖已有文件。

**转 GIF**：

```sh
ffmpeg -n -i clip1.mp4 -vf "fps=12,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" clip1.gif
```

- **对用户说**：「已压到 xx MB：`talk_50mb.mp4`；GIF 是 `clip1.gif`（480 宽、12fps、xx MB），想更清楚或更小可以调这两个值。」

## N3

**首尾拼接**：

```sh
ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,sample_rate -of json a.mp4
ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,sample_rate -of json b.mp4
printf "file '%s'\n" "$PWD/a.mp4" "$PWD/b.mp4" > /tmp/concat.txt
ffmpeg -n -f concat -safe 0 -i /tmp/concat.txt -c copy ab.mp4
# 参数不一致时改用 concat 滤镜重编码：
# ffmpeg -n -i a.mp4 -i b.mp4 -filter_complex "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1" ab.mp4
```

**顺时针转 90 度**：

```sh
ffmpeg -n -i v.mp4 -vf transpose=1 -c:a copy v_rot90.mp4
```

- **怎么判断成功**：拼接结果的时长等于 a 和 b 之和。旋转结果的宽高互换了。
- **对用户说**：「已拼接成 `ab.mp4`；已旋转成 `v_rot90.mp4`。」

## N4

```sh
ffmpeg -n -i talk.mp4 -vn -c:a libmp3lame -q:a 2 talk.mp3
```

- **对用户说**：「已提取 `talk.mp3`（VBR 约 190kbps）。」

## N5

这和视频无关，不用 autocut。

- **步骤**：用 Read 读 `README.md`，用 Edit 逐处改错别字，再用 `git diff README.md` 自查。按你的习惯，改完就 commit：`docs: fix typos in README`。
- **对用户说**：「改了 N 处错别字（列出来），已提交。」

## N6

```sh
autocut transcribe talk.mp4 --format srt --json
```

- **看什么**：输出的 srt 路径。如果返回退出码 4（缺语音识别模型），就跑 `autocut doctor`，把 `fix` 转告用户。
- **对用户说**：「字幕已生成：`talk.srt`，共 N 条，覆盖 mm:ss。」

## S1

```sh
ls -l talk.autocut.json                 # 方案文件按约定就在素材旁边
autocut show --help                     # 看能不能按时间定位，也看时间是按原片还是按成片算
autocut show talk.autocut.json --json   # 找覆盖 192s 的那一刀（或者用 --at 192 之类的参数）
```

- **先弄清时间基准**：用户说的 3:12 很可能是在成片里看到的，成片时间和原片时间不一样。我会先问清楚，或者用 show 换算后把两种时间都列给用户。
- **找到后**：读出那一刀的 id、理由和把握度，跟用户说明。用户确认要恢复后，再跑：

```sh
autocut set talk.autocut.json <id> --keep --json
```

  然后确认 revision 加了 1。
- **重新出片**：昨天的成片已经存在，默认不覆盖，用 `--force` 会覆盖原文件。所以我会先问用户：是覆盖，还是输出成新文件名。
- **对用户说**：「原片 mm:ss（成片 3:12）处剪掉了 X 秒，理由是"……"，把握度 0.xx。已改为保留。要我重新出片吗？覆盖旧成片还是另存？方案文件：`talk.autocut.json`。」

## S3

```sh
# 从我自己启动的后台任务的 stderr 里找 pid=N
kill N
ps -p N                                 # 确认进程已经退出
ls -l <预期成片路径>                     # 看有没有留下半成品
```

- **处理半成品**：如果留下了不完整的文件，告诉用户，并说明下次出片前需要处理它，否则不加 `--force` 就写不了。不用 pkill 或 killall。
- **对用户说**：「已停止出片（pid N 已退出）。磁盘上留下了一个不完整的 xxx.mp4，方案没受影响，随时可以重新 render。」

## H5

```sh
autocut highlights --help               # 确认没有 --chat 时能不能跑
autocut highlights vod2.mp4 -n 3 --json
autocut show vod2.autocut.json --filter uncertain --json
```

- **看什么**：`summary` 里实际找到的条数，以及 `notes`。只找到 2 条时，我不降低标准，也不自己挑第 3 段补满。
- **对用户说**：「只找到 2 条够格的高光（时间段 + 理由），没有凑第 3 条。如果你心里有想要的片段，告诉我时间，我加进去。有弹幕文件的话，加上 `--chat` 可能找得更准。方案文件：`vod2.autocut.json`。」

---

## 困惑之处

1. **命令参考被截断**：说明在"命令一览"那里断了，各命令的参数我都看不到，只能靠 `--help` 补。下面几条疑问很多都源于此。
2. **`--pace relaxed` 的含义不清**：它是说"讲话人节奏慢"（停顿阈值放宽），还是说"剪得少"？T1 的"别剪太碎"需要的是后者，比如最短保留片段、合并相邻刀口。现在不清楚有没有这类参数，`relaxed` 这个名字两种理解都说得通。
3. **两个 `say` 撞名**：export 输出里的 `say` 字段是"给用户转述的话"，而退出码 4 里的"say 的声音"指的是 macOS 的 TTS。同一个词表示两件事，容易误导。
4. **游戏实况怎么处理没写**：多音轨该用哪条判断有没有说话，没有说明。"没看点"和"打怪"是怎么识别的，也不知道。
5. **高光的输出形态没写**：竖屏、字幕属于 highlights 的参数还是 render 的参数？3 条高光是一个方案出 3 个文件吗？`--chat` 必填吗？jsonl 的字段格式是什么？
6. **多输入时方案文件叫什么**：`<素材>.autocut.json` 只适用于单个素材。montage 有 6 个输入、narrate 的输入是 md 加图片，方案文件叫什么不明确，我只能从 JSON 输出里取。
7. **同一素材重跑会怎样**：T1、T3、T4 都会写 `talk.autocut.json`。重跑 cut 是覆盖、加 revision，还是会丢掉之前用 `set` 做的修改？没有说明。
8. **show 的时间基准**：show 能按时间查吗？时间是原片时间还是成片时间？这直接决定 S1 能不能做对。
9. **export 格式和软件的对应**：fcpxml、xmeml、edl 分别对应哪款软件没写。达芬奇该用哪个、FCPXML 是哪个版本，都不清楚。
10. **ffmpeg 的适用范围**：skill 只列了转码、截取、拼接、压缩可以直接用 ffmpeg。查信息、转 GIF、旋转、提取音频这些没提到，我是推断它们也属于"直接用 ffmpeg"。
11. **uncertain 的标准**：`--filter uncertain` 的阈值是多少？能调吗？
12. **取消后的半成品**：kill 之后会不会留下半成品？如果留下了，"不覆盖已有文件"会不会挡住下一次 render？
13. **narrate 和 ytpmv 的指定方式**：narrate 里图片怎么对应段落？ytpmv 怎么指定"这一句"台词、怎么加字卡？都没有说明。
14. **render 核验的内容**：render 说"自带核验"，但没说核验什么，所以我不知道还需要自己补查哪些项目。