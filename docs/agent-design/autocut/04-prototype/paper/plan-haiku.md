# 任务规划（未执行）

**共同约定**：每一步都先看退出码和 JSON 的 `ok`，再看 `notes` 和 `summary.degraded`。`next` 只作参考，不照抄。下面的字段名和输出结构来自 skill 文本，实际输出要以运行结果为准。

## T1 长停顿剪掉，别剪太碎

```bash
autocut cut talk.mp4
autocut show talk.autocut.json --filter uncertain
autocut review talk.autocut.json      # 用户在浏览器里逐条核对
autocut render talk.autocut.json
```

- **看什么**：`summary` 里的切点数和总剪掉秒数。如果切点大多很短（如不到 0.5 秒），说明太碎，就用 `autocut set` 把短切点改成 `--keep`，不手改 JSON。
- **成功**：三条命令退出码为 0，方案文件存在，`render` 自带的核验通过。
- **告诉用户**："已剪掉 N 处长停顿，共 X 秒；拿不准的 k 处见 review 页面。"

## T2 教室录课，去掉没人说话的部分

```bash
autocut cut lecture.mp4 --pace relaxed
autocut render lecture.autocut.json
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 lecture.mp4
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 lecture_cut.mp4   # 实际文件名以 render 输出为准
```

- **看什么**：从 `summary` 读出剪掉的秒数和比例，再用两次 `ffprobe` 的时长相减核对。
- **成功**：两个时长之差和 `summary` 报告的剪掉量一致（误差在 1 秒内）。
- **告诉用户**："原长 X，成片 Y，剪掉 Z（约 P%）。"
- **注意**：教室里的老师停顿也可能被当成"没人说话"剪掉，要在结果里提醒用户。

## T3 去掉说错重来的段和"嗯"

```bash
autocut doctor                       # 先确认语音识别模型在不在
autocut cut talk.mp4
autocut show talk.autocut.json --json   # 检查切点理由里有没有"重说""口头禅"
autocut render talk.autocut.json
```

- **看什么**：`doctor` 的结果。如果缺语音识别模型（退出码 4），把 `error.fix` 原样告诉用户，不自己下载，也不去磁盘上搜模型。
- **成功**：`summary.degraded` 为 false，切点理由里出现"重说"和"口头禅"，`notes` 里没有"没检测重说/口头禅"。
- **告诉用户**：分别列出重说段和"嗯"的数量。如果是降级运行，要明确说没做重说检测。

## T4 不渲染，导出 FCP 和达芬奇工程

```bash
autocut cut talk.mp4
autocut export talk.autocut.json --format fcpxml
autocut export talk.autocut.json --format xmeml
```

- **看什么**：两条命令退出码为 0，工程文件存在；读取 JSON 里的 `say` 字段。
- **成功**：生成两个工程文件，没有 `render` 产生的成片。
- **告诉用户**：用 `say` 字段的原话转述，比如"已导出 FCPXML 和 xmeml 工程"。不说"能导入 FCP/达芬奇"，因为还没在软件里打开过。

## T5 游戏实况：去掉没话没看点的段，保留打怪

```bash
autocut cut --help                   # 确认有没有选音轨的参数
ffprobe -v error -show_entries stream=index,codec_type,codec_name:stream_tags=title -of json game.mp4
autocut cut game.mp4 [音轨参数，如果 help 里有]
autocut show game.autocut.json       # 逐段看理由，找出打怪段
autocut set game.autocut.json <id> --keep   # 对每个打怪段执行
autocut review game.autocut.json
autocut render game.autocut.json
```

- **看什么**：ffprobe 列出的音轨，确认哪条是人声（麦克风），哪条是游戏声。
- **成功**：打怪段的 id 全部被设为 keep，`show` 里确认它们都是保留状态。
- **注意**：autocut 按"没话/无聊"判断，它不认识"打怪"。打怪段要靠我从 review 时间轴或抽帧看出来（`ffmpeg -ss <t> -i game.mp4 -frames:v 1 /tmp/f.jpg` 后读图）。这一步拿不准的，交给用户确认。

## T6 直播录像切 3 条高光，竖屏带字幕

```bash
autocut highlights vod.mp4 --chat chat.jsonl -n 3
autocut review <高光方案文件>                # 从 highlights 的输出里取路径
autocut render <高光方案文件>                # 每条都要渲染
# 竖屏：中心裁切后放大到 1080x1920（按画面内容决定是否要模糊背景）
ffmpeg -n -i clip1.mp4 -vf "crop=ih*9/16:ih,scale=1080:1920" clip1_v.mp4
# 字幕：对成片转写，再烧录
autocut transcribe clip1.mp4 --format srt
ffmpeg -hide_banner -filters | rg subtitles          # 先确认 ffmpeg 支持字幕滤镜
ffmpeg -n -i clip1_v.mp4 -vf "subtitles=clip1.srt" clip1_final.mp4
```

- **看什么**：`highlights` 的退出码和 `notes`。如果 `notes` 写了"只找到 k 条"，就如实告诉用户，不凑数。
- **成功**：每条成片都是 9:16，字幕可见，文件名不覆盖已有文件。
- **不做**：不上传到抖音，只给本地路径。

## T7 6 段素材配 music.wav 做卡点混剪

```bash
ls clips/*.mp4 | wc -l               # 应该是 6
ls -l music.wav
autocut montage clips/*.mp4 --music music.wav
autocut show <montage 方案文件>         # 文件名以输出为准
autocut review <montage 方案文件>
autocut render <montage 方案文件>
```

- **看什么**：镜头数是否为 6，`summary` 里的节拍数，切点是否对齐节拍。
- **成功**：每个切点都对应节拍，成片时长和音乐一致。

## T8 按 script.md 和 3 张图做知识视频（配音、字幕、配图）

```bash
autocut doctor                       # 确认中文 say 声音和识别模型
autocut narrate script.md img1.png img2.png img3.png
autocut show <narrate 方案文件>
autocut review <narrate 方案文件>
autocut render <narrate 方案文件>
```

- **看什么**：`doctor` 里 `say` 的声音是否可用（缺失则按 `fix` 告诉用户）；`notes` 里是否写了文稿段落数和图片数不一致。
- **成功**：配音、字幕、配图都在方案里，图片和段落的对应关系符合预期。

## T9 鬼畜：台词配 music.wav，字卡在拍子上

```bash
autocut doctor                       # 字卡文字依赖转写，先确认识别模型
autocut ytpmv line.mp4 --music music.wav
autocut show <ytpmv 方案文件>          # 对比字卡时间和节拍点
autocut set <ytpmv 方案文件> <id> --keep|--cut   # 需要时调整
autocut review <ytpmv 方案文件>
autocut render <ytpmv 方案文件>
```

- **看什么**：字卡的起始时间是否落在节拍点上（允许很小的误差）；`doctor` 结果。
- **成功**：字卡都贴拍，`notes` 没有降级提示。如果字卡不贴拍，要说明原因，不硬改。

## N1 talk.mp4 多长？什么编码？

```bash
ffprobe -v error -show_entries format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate -of json talk.mp4
```

- **告诉用户**：时长（秒和 mm:ss）、视频编码（如 h264）、音频编码（如 aac）、分辨率和帧率。

## N2a 把 talk.mp4 压到 50MB 以内（两遍编码，目标按 50×10⁶ 字节算）

```bash
set -euo pipefail
D=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 talk.mp4)
VKBPS=$(awk -v d="$D" 'BEGIN{printf "%d", (50000000*8*0.97/d/1000) - 128}')
ffmpeg -n -i talk.mp4 -c:v libx264 -b:v ${VKBPS}k -pass 1 -an -f null /dev/null
ffmpeg -n -i talk.mp4 -c:v libx264 -b:v ${VKBPS}k -pass 2 -c:a aac -b:a 128k talk_50mb.mp4
stat -f%z talk_50mb.mp4              # 应 ≤ 50000000
rm -f ffmpeg2pass-0.log* ffmpeg2pass-0.log.mbtree   # 临时文件，只删自己生成的
```

- **成功**：文件大小 ≤ 50,000,000 字节，`ffprobe` 时长和原片接近。

## N2b 把 clip1.mp4 转成 GIF

```bash
ffmpeg -n -i clip1.mp4 -vf "fps=12,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer" clip1.gif
stat -f%z clip1.gif
```

- **成功**：GIF 生成，大小如实报告。如果太大，就降低 fps 或宽度后重做。

## N3a 把 a.mp4 和 b.mp4 首尾拼接

```bash
ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,pix_fmt,sample_rate -of json a.mp4 b.mp4
# 参数一致 → 无损拼接
printf "file '%s'\n" "$PWD/a.mp4" "$PWD/b.mp4" > /tmp/concat.txt
ffmpeg -n -f concat -safe 0 -i /tmp/concat.txt -c copy ab.mp4
# 参数不一致 → 重新编码
ffmpeg -n -i a.mp4 -i b.mp4 -filter_complex "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]" -map "[v]" -map "[a]" -c:v libx264 -c:a aac ab.mp4
```

- **看什么**：两个文件的编码参数是否一致。如果分辨率不同，`concat` 滤镜前要先 scale。
- **成功**：ab.mp4 时长约等于两段之和。

## N3b 把 v.mp4 顺时针转 90 度

```bash
ffprobe -v error -show_streams v.mp4 | rg -i "rotat"    # 先看有没有旋转元数据
ffmpeg -n -i v.mp4 -vf transpose=1 -c:a copy v_rot90.mp4
ffprobe -v error -select_streams v:0 -show_entries stream=width,height v_rot90.mp4
```

- **看什么**：如果源文件已有旋转元数据，ffmpeg 会自动旋转，再加 transpose 就转了两次，要去掉一次。
- **成功**：宽高互换，画面方向正确。

## N4 把 talk.mp4 的音频提成 mp3

```bash
ffmpeg -n -i talk.mp4 -vn -c:a libmp3lame -q:a 2 talk.mp3
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 talk.mp3
```

- **成功**：两个时长相差在 0.1 秒内。

## N5 把项目 README 的错别字改掉

```bash
ls *.md                              # 找到 README
```

- 用 Read 读 README，列出错别字，用 Edit 修改（不要用 sed）。
- `git diff README.md` 检查改动，然后 `git add README.md` 和 `git commit -m "docs: 修正 README 错别字"`（按 git 规则随时提交）。只 add 这一个文件。
- **不做**：不碰当前目录里的 mp4，不跑 autocut。

## N6 只要 talk.mp4 的字幕，不剪

```bash
autocut transcribe talk.mp4 --format srt
```

- **看什么**：退出码。如果是 4（缺识别模型），按 `fix` 告诉用户。
- **成功**：srt 文件存在，时间戳递增，内容非空。
- **告诉用户**：字幕文件的完整路径。

## S1 新会话：恢复昨天 talk.mp4 在 3:12 被剪的段

```bash
ls -la talk*.autocut.json            # 先在工作区内找方案文件
autocut show talk.autocut.json --json   # 找到覆盖 192.0 秒的那一条，读它的 reason 和 id
autocut set talk.autocut.json <id> --keep
```

- **如果找到方案**：把这一段改为保留，告诉用户被剪的理由（取自方案里的 reason）。改动会让 revision 加 1。如果成片要重新生成，`render` 会因为不覆盖已有文件而需要新文件名或 `--force`，要先问用户。
- **如果找不到方案**：不能确认当初的理由，不能编造。告诉用户源文件 talk.mp4 没动。重新 `autocut cut talk.mp4` 会得到一份新方案，结果可能不同，要先征得用户同意。

## S3 停掉正在跑的 10 分钟出片

```bash
# 从之前后台任务的 stderr 里找 pid=N
ps -p N -o pid,ppid,etime,command    # 确认是 autocut render，且是我启动的
kill N                               # 不用 pkill / killall
sleep 5
ps -p N                              # 应该没有输出
ls -l <成片路径>                      # 查看是否有半成品
trash <半成品路径>                    # 只删本次出片产生的半成品
```

- **先确认归属**：同一仓库里还有两个兄弟会话（autocut-7f、autocut-31）。如果 `ps` 显示的进程不是我启动的，不要杀，告诉用户。
- **成功**：进程已退出，半成品已移入废纸篓，方案文件没有改动。

## H5 vod2.mp4 切 3 条高光（实际只有 2 段精彩）

```bash
autocut highlights vod2.mp4 -n 3
```

- **看什么**：`notes` 和 `summary`，预期会写明只找到 2 条。
- **渲染**：只渲染这 2 条，不把弱候选补进来。
- **告诉用户**："只找到 2 条，第 3 条不足，没有补。"如果用户想放宽标准，由用户决定，不擅自降低阈值。

---

## 困惑之处

1. **`--pace` 取值含义不明**：摘要只说讲课用 `relaxed`，没说明它保留多少停顿。T1 的"别剪太碎"和"长停顿"如何对应，我拿不准。
2. **命令参考被截断**：表格在"改文件"处断了，`cut`、`highlights`、`narrate`、`ytpmv` 等命令的完整参数我看不到，只能靠 `--help` 确认，比如 T5 的音轨参数。
3. **同名方案文件冲突**：T1、T3、T4 都对 talk.mp4 操作，方案文件都是 `talk.autocut.json`，不能同时存在。我假设它们是独立的会话或分开执行。
4. **"达芬奇"对应哪种格式不明**：skill 列出的是 xmeml、fcpxml、edl。我计划两种都导，但不确定达芬奇哪种导入最稳。
5. **T5 的"打怪"无法靠 autocut 判断**：autocut 的依据是"没话/无聊"，它不知道什么是打怪。这要靠我看画面或让用户确认。
6. **T6 的竖屏和烧字幕不在摘要里**：autocut 没有列出竖屏或烧录字幕的命令，我计划用 ffmpeg 处理，并且假设 ffmpeg 支持 subtitles 滤镜（要先验证）。
7. **T7 的"鼓点"和"卡点"**：不确定 montage 是按鼓点检测，还是只按固定 BPM 切。
8. **T8 的 narrate 与"知识视频"**：不清楚 narrate 是否会自己生成字幕，图片和段落如何配对。
9. **T9 的字卡来源**：ytpmv 的摘要没提字卡的文字从哪里来，我假设依赖转写。
10. **S3 的 pid 归属**：`pid=N` 只出现在 stderr 里，无法从输出确认它是不是我启动的。
11. **H5 没提弹幕**：用户没给 `--chat`，我假设 highlights 在没有弹幕时仍能运行，但不确定。