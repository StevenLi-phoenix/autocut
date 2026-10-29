# Baseline 观察：没有 autocut 时，agent 怎么剪视频

阶段：Empathize · 日期：2026-10-28 · 范围：只观察，不聚类，不提方案（Define 阶段再做）。
原始证据：`evals/runs/<runset>/*.jsonl`（stream-json 全文，gitignored，只在磁盘）、`*-debrief.jsonl`、`*-verify.json`、`metrics.md`。
读法：卡片都从 agent 的视角写它**相信了什么 / 看到了什么**；`(suggestion)` 是 agent 自己在 debrief 里提的建议，只当假设。

本文件分两章：**第一章 批 1（T1–T4，已完成）**；**第二章 批 2（T5–T9，各视频类型，见文末）**。

---

# 第一章 批 1：口播类四个任务

## 1.1 方法

**环境**
- macOS arm64；ffmpeg/ffprobe 9.0.2；`claude` CLI 2.1.303；被测模型 `--model opus`（解析为 claude-opus-5-5）和 `--model haiku`（claude-haiku-5-5）。
- 素材：`fixtures/media/talk_zh.mp4`（中文 TTS，40.7s，底噪 -52dBFS）、`lecture_en.mp4`（英文，35.3s，-38dBFS 粉噪），旁边各有 `*.truth.json`（每段 speech/gap 的精确区间，tag = filler / retake）。生成脚本 `fixtures/make_fixtures.py`。
- 被测 agent 的 cwd 是一次性沙盒 `…/scratchpad/evalwork/baseline-2026-10-28/<task>-<model>-<n>/`，只放一份 mp4 副本，不放 truth.json。
- 被测 agent 继承用户的全局 Claude Code 配置（这是真实使用场景，不是为 autocut 准备的）：init 事件显示 71 个 skill、14 个 MCP server、107 个工具（另有若干 case 只有 26 个，见卡片），首轮上下文约 41k token；全局 CLAUDE.md 与 rules 会加载（后面有卡片显示 Python 规范渗进了剪辑任务）；`CLAUDE_EFFORT=xhigh` 从环境继承。没有任何视频相关 skill。
- 启动脚本清掉了父会话身份环境变量（`CLAUDECODE`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_MESSAGING_*`、`CLAUDE_CODE_BRIDGE_SESSION_ID`、`CLAUDE_CODE_PLUGIN_DIRS` 等），避免被测 agent 连上当前会话的消息通道。

**批 1 的有效性缺口（重要）**：批 1 的沙盒只是 cwd 在仓库外，**文件系统没有隔离**。证据见卡片 C1–C2、C11、C31、C34–C35、C40、C45、C49、C63、C74：agent 用 `find / -name "talk_zh*"`、`find ~ /opt/homebrew -name 'ggml-*.bin'`、`mdfind` 搜到了仓库 `fixtures/`、scratchpad 里的 calib 文件，以及仓库 `models/` 下的 whisper 模型并直接使用；cwd 路径里的 "autocut" 字样让 agent 去 `command -v autocut`、`ls ~/Codes/autocut`。没有 agent 读过 truth.json 的内容（已 grep 全部 transcript：只有 `find` 的文件名列表里出现）。另外 fixture 画面左下角烧录了 "autocut fixture talk_zh"，看帧的 agent 读到了。**所以批 1 的 T3/T4 结论要按"agent 可以用到本机 whisper-cli + 仓库里的模型"来读**；批 2 起被测 agent 改在 macOS Seatbelt（`sandbox-exec`）里跑，读不到仓库和 scratchpad 其余目录（见第二章方法）。

**命令**（`evals/run_headless.sh`；批跑入口 `evals/run_matrix.sh`，`xargs -P 4`）
```
cd <沙盒> && timeout 900 claude -p "<prompt>" --model <opus|haiku> --output-format stream-json --verbose \
    --dangerously-skip-permissions --max-turns 60 < /dev/null > evals/runs/baseline-2026-10-28/<case>.jsonl
claude -p "<debrief>" --resume <session_id> --output-format stream-json --verbose \
    --dangerously-skip-permissions --max-turns 3 > evals/runs/baseline-2026-10-28/<case>-debrief.jsonl
```
4 个任务 × 2 个模型 × 2 次 = 16 个 case，原始 transcript 先落盘再分析。

**任务 prompt 原文**（`evals/baseline/tasks.json`；每个末尾统一追加后缀）
- T1 long_pauses：`talk_zh.mp4 是我录的一段口播，帮我把说话中间那些长时间的停顿剪掉，正常的短停顿留着，别剪得太碎。输出成 talk_zh_cut.mp4。`
- T2 noisy_silence：`lecture_en.mp4 是在比较吵的教室里录的课，帮我把没人说话的部分去掉，输出 lecture_en_cut.mp4，顺便告诉我一共剪掉了多少时间。`
- T3 retake_filler：`talk_zh.mp4 里有一处我说错了又重新说的地方，还有个“嗯”，帮我把说错的那段和口头禅剪掉，输出 talk_zh_clean.mp4。`
- T4 export_nle：`帮我把 talk_zh.mp4 粗剪一下（去掉长停顿），但先别渲染成片，导出一个 Final Cut Pro 或 DaVinci Resolve 能打开的工程/时间线文件，我要自己再微调。`
- 统一后缀：`（工作时请简要说明你选每一步的理由；有任何拿不准的地方也顺手说出来。注意：当前网络按流量计费，不要下载模型、不要安装新的软件包。）`
- debrief 原文：`刚才这个任务结束了，请简短回答，每条 1–2 句：1) 哪一步你最没把握，为什么？2) 你是怎么确认输出是对的？哪些地方没有确认？3) 过程中哪些命令的输出让你困惑、太长或者没用（如果有）？`

**校验器**（`evals/verify.py`，27 个测试 `tests/test_verify.py` 全过）：把源和产物音频解成 16kHz mono、300–3400Hz 带通，对 truth 里每段 speech 在产物里做归一化互相关；≥0.8 intact，<absent 阈值 absent，之间 partial；absent 阈值 = max(0.3, 该模板在源里排除自身落点后的最高误匹配分数 + 0.03)（短口头禅“嗯”会误匹配到 0.5 左右，固定 0.3 会把剪干净的产物判成 partial——实测标定后改的）。另算 coverage（intact 但头尾被削记 clipped）、相邻存活片段间隔（>1.0s 死寂、<0.08s 挤在一起）、音画时长差 ≤0.15s。T4 解析 FCPXML / xmeml / EDL / OTIO，查源文件名引用、总时长在 [无 tag 语音总长, 原始时长-1.0s]，另加两项：该保留的语音在时间线源区间里被覆盖 ≥90%、长停顿在时间线里保留 ≤1.0s。

## 1.2 结果总览

| task | model | pass | 平均调用 | 平均 $ | 说明 |
|---|---|---|---|---|---|
| T1 long_pauses | haiku | 2/2 | 3.5 | 0.01 | |
| T1 long_pauses | opus | 2/2 | 9.5 | 0.78 | |
| T2 noisy_silence | haiku | 2/2 | 9.5 | 0.02 | 剪掉 11.7s / 9.5s（opus 11.7–12.3s） |
| T2 noisy_silence | opus | 2/2 | 13.0 | 0.78 | |
| T3 retake_filler | haiku | **0/2** | 13.5 | 0.03 | “首先你需要准备好原始素材”被切进去，“嗯”还在 |
| T3 retake_filler | opus | 2/2 | 10.5 | 0.65 | |
| T4 export_nle | haiku | 2/2 | 9.5 | 0.02 | 都没能在 FCP/Resolve 里打开验证 |
| T4 export_nle | opus | 2/2 | 16.5 | 1.11 | 同上 |
| 合计 | haiku | 6/8 | 9.0 | 0.02 | 总 $0.17 |
| 合计 | opus | 8/8 | 12.4 | 0.83 | 总 $6.65 |

逐 case 指标（调用/Bash/错误/$/秒/最大单次输出字符）见 `evals/runs/baseline-2026-10-28/metrics.md`。「自验」列已按 transcript 人工核对（`self_verify.json`，表中带 `*`）：16 个 case 全部做了某种复核，但复核手段高度同质（见卡片 C70）。

## 1.3 逐 case 轨迹摘要

**T1-haiku-1**（PASS · 4 调用 · $0.01 · 111s）第一步就 `ls -la /Users/lishuyu/Codes/autocut` 并 `find / -name "talk_zh*"`（cwd 里已有该文件）；之后 `silencedetect=noise=-30dB:d=0.3` 看到 4 处 ≥1s 的停顿，决定“≥1s 的两侧各留 0.25s、压成约 0.5s”，一条 trim/atrim+concat 命令渲染，对成片重跑 silencedetect 看最长剩余 0.83s，结束。debrief 承认 -30dB 没实测、没确认切点是否落在字间和音画同步。

**T1-haiku-2**（PASS · 3 调用 · $0.01 · 32s）解到 /tmp 的 wav 上 -35dB 初筛，判 0.6–0.8s 为自然停顿保留，≥1s 两侧各留 0.3s；一条命令渲染并对成片复测。最短路径，没测噪声。

**T1-opus-1**（PASS · 9 调用 · $0.75 · 103s）先测 100ms RMS 包络（输出 7.2k 字符）→ 双峰 → -40dB；发现 31.46–34.87 被 1ms 杂音劈成两段，脚本里加合并；写 `cut_pauses.py`，再用一段 python 按全局 Python 规范改成 logging+类型标注、`uv venv`；dry-run → 渲染 → 复测 → 抽帧拼图并 Read，借画面烧录的时间码核对剪点。最终回复列了 4 条“拿不准”。

**T1-opus-2**（PASS · 10 调用 · $0.81 · 119s）包络输出 13,989 字符；zsh 下 `set -- $r` 循环两次输出为空；单独核对 32.4–33.3s 那段“-47dB 抬升”的峰值/均值，判为呼吸；写脚本、补类型标注、`uv venv`、渲染、复测（16 处停顿最长 0.81s，非静音总长 26.71→26.83s）。debrief 补了一条最终回复里没写的差异：24.45s 的停顿剪后只剩 0.384s。

**T2-haiku-1**（PASS · 9 调用 · $0.02 · 79s）silencedetect 在 -30/-35/-40dB 全空→退出码 1→认为底噪高于 -30dB，扫到 -25dB；写 plan_cut.py + apply_cut.py，自己抓到两个自己引入的问题（0.3s 假保留段、合并逻辑留下的 0.54s 残留）并修掉；声称剪掉 11.8s（实测 11.74s）。

**T2-haiku-2**（PASS · 10 调用 · $0.02 · 79s）silencedetect 在 -32/-36dB 无输出，怀疑 ffmpeg 报错，查明后改用 Python 标准库算 100ms RMS，选 -30dB；zsh 下 `for s in $SEG` 把参数拼坏；成片复测仍有 1.0s 连续静默，解释为句间停顿并接受；声称 9.54s（实测一致，但比 opus 少剪 2–3s）。

**T2-opus-1**（PASS · 9 调用 · $0.73 · 115s）300–3400Hz 带通后测噪声（一次性输出 18.6k 字符）；脚本版 silencedetect 为空、只回 “Exit code 1”，`bash -x` 重跑才看明白，改自己做 50ms RMS 分段；awk 未初始化变量使第一段起点为 -0.150，肉眼读数发现；复测人声频段 >-40dB 的片段数剪前剪后同为 390；声称 11.68s（一致）。

**T2-opus-2**（PASS · 17 调用 · $0.83 · 249s）首条命令 `cd` 路径写成连字符形式失败；带通后测噪声（16k 字符）；9.2s/33.7s 两段短声用频谱图 + Read PNG 判断为人声；启动 `find ~ /opt/homebrew -name 'ggml-*.bin'` 找 whisper 跑了两分多钟，用 TaskStop 停掉；脚本补 logging/类型标注、`uv venv`；声称 12.29s（计划值，成片实测 12.27s，debrief 自己指出）。

**T3-haiku-1**（FAIL · 11 调用 · $0.03 · 272s）`find /` 找视频、`command -v whisper whisper-cli…`，发现仓库 `models/` 里的 ggml-large-v3-turbo；用 whisper token 时间戳把“嗯”定在 12.84–13.23s（真值 14.51–14.74s），据此切掉 12.77–13.30（“素材”的尾部）和 20.10–27.60；对成片再转写看到“素材,按,然后”，判“按”是“材”的尾音；最终回复“嗯已剪掉”，附“按”待确认。实测：speech#2 partial(0.74)，filler 仍 intact(1.0)。

**T3-haiku-2**（FAIL · 16 调用 · $0.03 · 214s）同样用仓库里的 q5_0 模型；第一版成片转写仍有“嗯”→认为字级时间戳偏早，把切点 12.80→12.43，DTW 模式时间戳不变；最终转写“嗯”消失、拼接处冒出“按”，判定“不是剪切造成的”；声称成功。实测与 haiku-1 相同（partial 0.66 + filler intact 0.996）。

**T3-opus-1**（PASS · 10 调用 · $0.63 · 137s）`find ~ /opt/homebrew -maxdepth 6` 找到同一模型；发现 token 时间戳与静音区间整体偏 1–2s，不再信它，把静音切出的“语音岛”逐个单独转写，核实 14.51–14.73（0.22s）是“嗯”，还去掉 prompt 交叉验证防止被诱导；剪 14.30–15.03 和 20.30–27.60；对成片整片重转写 + 静音检测复核。

**T3-opus-2**（PASS · 11 调用 · $0.68 · 118s）`mdfind -name ggml` 找到模型；whisper 把“嗯”标在 13.74s、“第二步是把没用的”七字同标 21.94s；用 20ms 能量包络找到 14.50–14.74 孤立短音并单独转写确认；剪 14.30–15.20 和 22.10–27.80；抽帧 Read 后认为“测试图，跳帧不影响”；成片重转写（同一模型）。

**T4-haiku-1**（PASS · 12 调用 · $0.03 · 202s）选 FCPXML 1.10（“FCP 和 Resolve 都能导入”），脚本生成时间线 + JSON 剪点表；自己写的检查抓到“尾部静音没剪干净”并修复；复核命令里 `-map [out]` 标签已被消费报错 234，判为自己命令写错；用 xmllint + 内存拼接后复测静音。从未在 NLE 里打开。

**T4-haiku-2**（PASS · 7 调用 · $0.02 · 147s）选 xmeml（FCP7 XML，“能保留音视频 link”），5 个视频片段 + 5 个音频片段；解析自己的 XML 核对连续性 + 内存拼接复测。debrief 自评 `|| true` 会吞真错误。

**T4-opus-1**（PASS · 19 调用 · $1.18 · 241s）先 `command -v … auto-editor`、查 `/Applications` 里有无 FCP/Resolve（没有）；导出 FCPXML 1.8 + EDL + CSV；按全局规则 `uv venv` + `uv pip install pytest --offline`（离线缓存）写 8 个 pytest；把 `143/15s` 改写成 `286/30s`（“FCP 惯用分母”）。debrief：format 无 name、`src` vs `media-rep`、DOCTYPE 写法都是按印象写的。

**T4-opus-2**（PASS · 14 调用 · $1.05 · 246s）`command -v autocut auto-editor`、`ls ~/Codes/autocut`；-45dB silencedetect 无结果，用“按采样峰值判断”解释但没验证，改 -35dB；导出 FCPXML 1.9 + EDL + CSV + 8 个 unittest + 重组音频 `_check_audio.wav`；抽帧对照 3 处晚 1 帧，归因取整，没重抽。

## 1.4 原始观察卡片（批 1）

> 编号只是引用用，按 case 顺序排，不是分类。

**T1-haiku-1**
- C1 [T1-haiku-1] `ls` 已经看到工作目录里有 talk_zh.mp4，第一条命令仍然 `ls -la /Users/lishuyu/Codes/autocut` 并 `find / -name "talk_zh*"`：从 cwd 路径里的 “autocut” 推断项目目录存在，想确认工作目录里这份是不是“那份”。
- C2 [T1-haiku-1] `find /` 的输出（1.3k 字符）里同时有仓库 fixtures、scratchpad/calib 里的同名副本和 talk_zh.truth.json 的文件名；它认为“工作目录里的就是要处理的，其他副本不动”。debrief：这一步“可以省掉”。
- C3 [T1-haiku-1] 直接选 `silencedetect=noise=-30dB:d=0.3`，没有先测音量；debrief：“-30dB 是凭经验定的，没有用音频实测过”。这段录音底噪约 -52dBFS，所以恰好有效。
- C4 [T1-haiku-1] 把“对成片重跑同一个 silencedetect，最长剩余 0.83s”当作确认；debrief 承认没确认切点是否落在字间、轻声段是否被误切、音画是否严格同步。

**T1-haiku-2**
- C5 [T1-haiku-2] 同样选 -35dB 初筛，没测噪声；把 0.6–0.8s 的 4 处停顿“判为自然停顿保留”，自定“≥1s 才算长停顿”。
- C6 [T1-haiku-2] 整个任务 3 次调用，最终回复明说“只用音量判断，没有听过音频”，把“有意留的节奏停顿也会被剪”列为拿不准。

**T1-opus-1**
- C7 [T1-opus-1] 先测 100ms 一格的 RMS 包络（返回 7,232 字符，显示被截断），从“说话 -14~-23、停顿 -47~-56、中间 25dB 空档”选 -40dB，明确“不随意设 -30dB”。debrief：“每 100ms 一个的音量列表太长了，只需要静音段起止加一个分布”。
- C8 [T1-opus-1] silencedetect 把 31.46–34.87 的 3.4s 停顿劈成两段（中间一个 1ms 的小杂音），它认为那是同一段停顿，在脚本里加“间隔 <0.1s 合并”。debrief：“一开始容易被误读成两个停顿”。
- C9 [T1-opus-1] 看到 24.4s、32.5–33.2s “比底噪高 4–6dB 的很轻的声音”，当作呼吸/杂音一起剪掉或缩短；最终回复把这条列为拿不准，debrief：“只看了音量数值，没听过，不能排除是很轻的说话”。
- C10 [T1-opus-1] 脚本写完后“按本地 Python 规范”用一段 python 批量把 print 换成 logging、补类型标注，再 `uv venv -q .venv` 并运行；debrief：“这一步很笨，一开始就按规范写更干净”。（全局 CLAUDE.md/rules 的 Python 规范渗进了剪辑任务）
- C11 [T1-opus-1] 抽三个剪点前后的帧拼成 PNG 并 Read，读到画面里的烧录时间码和文字 “autocut fixture talk_zh”，用时间码核对三个剪点落在计划帧上；最终回复写“画面是彩条测试图，判断不了跳切明显不明显”。
- C12 [T1-opus-1] debrief 说“脚本在我跑完之后被改过，现在磁盘上的版本我没有重新跑”；transcript 里脚本的 python 批量改动发生在 dry-run 和渲染之前，之后只有 `rm -rf .venv`。（debrief 自述与记录不符）

**T1-opus-2**
- C13 [T1-opus-2] 包络命令返回 13,989 字符；debrief：“400 多行太长，只输出低于 -40dB 的区间就够了”。
- C14 [T1-opus-2] `for r in "9.3 11.1" …; do set -- $r; ffmpeg -ss $1 -to $2 …` 在 zsh 下输出全空，原样再跑一次仍为空，第三次才改写；debrief：“zsh 的 `set -- $r` 不会按空格拆分，ffmpeg 收到的是空参数”。
- C15 [T1-opus-2] 用“非静音总长 原片 26.71s / 成品 26.83s，没有吃掉任何字”作为不吃字的证据。
- C16 [T1-opus-2] debrief 补了最终回复里没有的事实：原片 24.45s 的 0.506s 停顿没剪，成品里测出来却只有 0.384s；它推测是重编码让阈值附近边界变了，“没有核实”。

**T2-haiku-1**
- C17 [T2-haiku-1] 第一次 silencedetect 在 -30/-35/-40dB 三个阈值下都没输出（末尾 grep 无匹配，整条命令退出码 1），它认为“底噪可能高于 -30dB”，把阈值扫到 -25/-20/-15，并用逐 0.5s RMS 交叉验证后选 -25dB。（debrief：“一开始误以为阈值检测失败，后来才发现是底噪高于 -30dB”）
- C18 [T2-haiku-1] 规划脚本的输出里有一个 0.3s 的“保留段”，它读数字发现是两段相邻静音之间 0.00002s 的浮点缝被 0.15s padding 放大；debrief：“靠读数字发现的，不是靠工具报错”。
- C19 [T2-haiku-1] 对第一版成片复测，发现残留一段 0.54s 停顿（6.24s），归因于自己加的 MERGE_GAP 合并逻辑，删掉后复测为空；最终 23.60s。（复测抓到了自己的 bug）
- C20 [T2-haiku-1] 最终回复写“共剪掉约 11.8 秒”；实测 11.74s。

**T2-haiku-2**
- C21 [T2-haiku-2] silencedetect 在 -32/-36dB 无输出，它一度以为 ffmpeg 报错，查看原始输出后认为“silencedetect 按峰值判断，教室瞬时噪声让安静段‘不算静音’”，改用 Python 标准库算 100ms RMS；debrief：“-28dB 的输出与 RMS 数据对不上，我没有深究就转向了 RMS”。
- C22 [T2-haiku-2] `for s in $SEG` 在 zsh 下只循环一次，ffmpeg 报错里的 `25nd=34.55` 很难一眼看出原因；它改写成 bash 脚本。
- C23 [T2-haiku-2] 成片上 RMS 复测还有 56 个窗口 <-30dB，查最长连续静默 1.0s，认为是句间停顿并接受；这个 case 只剪了 9.54s（opus 剪 11.7–12.3s）。
- C24 [T2-haiku-2] 最终回复“一共剪掉 9.54 秒”与实测一致；列出了被剪的 5 段原片时间。

**T2-opus-1**
- C25 [T2-opus-1] 带通 300–3400Hz 后测噪声（一次命令返回 18,613 字符），看到原始音轨 0.7s/13.5s 附近的 -32dB 杂音在带通后降回底噪，认为是低频桌椅声而不是说话。
- C26 [T2-opus-1] 脚本里的 silencedetect 一处静音都没找到，脚本只返回 “Exit code 1”；它用 `bash -x` 重跑才看明白，转为自己做 50ms RMS 分段。debrief：“那一轮基本白跑”。
- C27 [T2-opus-1] 第一段起点出现 -0.150；它读数字发现并查出是 awk 里未初始化的 `n` 当数组下标变成空串；debrief：“这是我自己写出的 bug”。
- C28 [T2-opus-1] 确认手段：人声频段内 >-40dB 的片段数剪前剪后都是 390 个，最长安静 0.45s，音画差 17ms；debrief：没听、没看画面。

**T2-opus-2**
- C29 [T2-opus-2] 第一条命令 `cd` 失败：路径写成 `…/-Users-lishuyu-Codes-autocut-457cba72-…`（连字符形式，像 Claude 项目目录名）而不是真实 pwd；`ls` 对比后改用相对路径。（T4-opus-1 也有同样的 `autocut-457…` 笔误）
- C30 [T2-opus-2] 单看音量分不清 9.2s（0.35s）和 33.7s（全片最响）那两段短声是字还是咳嗽，用 `showspectrumpic` 生成频谱图并 Read PNG，凭“谐波条纹”判断是人声而保留；debrief：“学生小声说话、笑声、哼声也有谐波，会被当成讲课”。
- C31 [T2-opus-2] 为找本地 whisper 模型启动 `find ~ /opt/homebrew -name 'ggml-*.bin'`，跑了两分多钟没结果；它用 ToolSearch 加载 TaskStop 停掉，改用频谱图。
- C32 [T2-opus-2] 最终回复“一共剪掉 12.29 秒”是按计划算的值；成片实测剪掉 12.27s，debrief 自己指出了这个差。
- C33 [T2-opus-2] 读到全局规则后“按 Python 规范”给脚本补 logging/类型注解并 `uv venv`，运行完 `rm -rf .venv`。

**T3-haiku-1**
- C34 [T3-haiku-1] 第一条命令就 `find / -name 'talk_zh*'` 并 `command -v whisper whisper-cli …; ls ~/.cache/whisper ~/.cache/huggingface`：它认为要先“听懂内容”才能剪口误和“嗯”，在找本机的转写工具。
- C35 [T3-haiku-1] 找到 whisper-cli 和 `/Users/lishuyu/Codes/autocut/models/ggml-large-v3-turbo.bin`，认为“本机已有、没有下载，可以直接用”；最终回复明写“模型直接用了 autocut 项目 models/ 下已有的文件”。
- C36 [T3-haiku-1] 相信 whisper 的 token 时间戳：把“嗯”定在 12.84–13.23s（真值 14.51–14.74s，偏早约 1.7s），据此切掉 12.77–13.30——那是“素材”的尾部——以及 20.10–27.60。
- C37 [T3-haiku-1] silencedetect 显示 12.8s 附近“嗯”前后没有停顿，它没有怀疑 whisper 时间戳整体偏移，转去用 10ms RMS 包络找“嗯”的边界，认定 12.84–13.23 的元音段就是“嗯”。
- C38 [T3-haiku-1] 对成片再转写看到“素材,按,然后”，担心“按”是“嗯”的残留，查能量后判“按”是“材”的尾音；最终回复一边写“‘嗯’已剪掉”，一边把“按”列为“需要你确认的一处”。（实测 filler 仍然完整在 14.5s）
- C39 [T3-haiku-1] debrief：“whisper 逐词时间戳很多 token 起止相同或明显错位，只能做大致定位”；“第二次转写文本也有误识别（‘准备使素材’），文本比对只能当粗略检查”。

**T3-haiku-2**
- C40 [T3-haiku-2] `ls -la /Users/lishuyu/Codes/autocut/models/` 后选 `ggml-large-v3-turbo-q5_0.bin`，转写产物放 /tmp/talkclean。
- C41 [T3-haiku-2] 第一版成片转写仍出现“嗯”，它判断“字级时间戳可能偏早”，把切点从 12.80 提前到 12.43，并用 `-dtw` 对齐——DTW 给出的时间戳与普通模式相同（仍标 12.80–12.98）；它又推断“12.43–12.80 的逗号其实是嗯的发声开头”。
- C42 [T3-haiku-2] 能量图与 whisper 字级时间戳“对不上”，它的结论是“whisper 的时间戳本身就粗”；debrief 称 RMS 输出“反而误导了判断”。没有去找能量图上 14.5s 的孤立 0.23s 短音。
- C43 [T3-haiku-2] 最终转写里“嗯”消失、拼接处冒出“按”；它判断“按”在输出 11.89s、在保留段内，“不是我的剪切造成的”，据此称成功。debrief：“可能是噪声也可能是‘嗯’的残留”。（实测 speech#2 partial 0.66，filler intact 0.996）
- C44 [T3-haiku-2] 把口误前 2.4s 的空白和口误一起剪掉（19.94–27.76），只保留 0.4s 自然停顿。

**T3-opus-1**
- C45 [T3-opus-1] 先 `command -v whisper whisper-cli mlx_whisper faster-whisper`，再 `find ~ /opt/homebrew -maxdepth 6 -name "ggml-*.bin" …`，选 `~/Codes/autocut/models/ggml-large-v3-turbo.bin`，理由“只读引用，不复制不改动；中文比 base 好”。
- C46 [T3-opus-1] `-ml 1 -sow` 只给出句子级结果，`-dtw` 静默失效（`t_dtw` 全是 -1，没有任何报错）；它发现 token 时间戳与静音区间“整体偏了 1–2 秒”，不再用 whisper 时间戳定切点。
- C47 [T3-opus-1] 把静音切出的“语音岛”逐个单独转写，核实 14.51–14.73（0.22s）是“嗯”；担心 prompt 里写了“嗯”会诱导 whisper，去掉 prompt 重转写并检查音量交叉验证。
- C48 [T3-opus-1] 对成片整片重转写，“嗯”、错的那句、“不对，我重新说一遍”都没了、其余一句不少；音频 32.707s、视频 32.700s。debrief：“没真的听过；‘嗯’的识别是靠 whisper 和音量两种方法交叉验证的，同样没人工听过”。

**T3-opus-2**
- C49 [T3-opus-2] `mdfind -name "ggml"` 找到仓库 models 下的模型，只读引用。
- C50 [T3-opus-2] whisper 把“嗯”标在 13.74s，“第二步是把没用的”七个字全标在 21.94s；它用 20ms 能量包络找到 14.50–14.74 的孤立短音，单独转写这一小段确认是“嗯”，剪点定在 14.30–15.20 和 22.10–27.80（接缝处约 -55dB）。debrief：“单独转写这么短的片段本身也不太可靠”。
- C51 [T3-opus-2] 抽四个剪点的帧 Read 后认定“画面是测试图，剪点跳帧不影响观感”。
- C52 [T3-opus-2] 成片视频流比音频短 1 帧，它先确认是不是原片自带的差异（是）；对成片再转写用的是同一个模型，debrief：“如果它两次都漏掉别的口头禅，我也发现不了”。

**T4-haiku-1**
- C53 [T4-haiku-1] 选 FCPXML 1.10，理由“FCP 和 Resolve 都能导入、能保留素材路径和 marker，优于 EDL”；只剪 ≥0.6s 的停顿，两端各留 5 帧，每个切点加 marker。
- C54 [T4-haiku-1] 自己写的一致性检查抓到 bug：文件末尾 silencedetect 给出了 `silence_end`，尾部静音分支没走，留了 0.17s 的碎尾段；它修了条件，时间线变成 7 段 31.57s。
- C55 [T4-haiku-1] 复核命令里 `-map [out]` 引用了已被 silencedetect 消费的标签，ffmpeg 退出码 234；它先判断“是我分析命令写错，不是素材问题”，去掉 `-map` 重跑。
- C56 [T4-haiku-1] 验证手段是 xmllint + 把保留段在内存里拼起来再跑 silencedetect（最长剩余 0.42s）；debrief：“从未在任一软件里打开过，最担心 Resolve 拒绝导入”。

**T4-haiku-2**
- C57 [T4-haiku-2] 选 xmeml（FCP7 XML），理由“比 EDL 能保留音视频的链接关系”；产物 5 个视频片段 + 5 个音频片段；写完先改掉自己觉得“绕的 link 逻辑和空循环”。
- C58 [T4-haiku-2] 验证：解析自己的 XML 核对每段 in/out 与时间线位置连续，再内存拼接复测（最长剩余 0.84s）。debrief：“XML 没在 FCP/Resolve 里导入过，音频声道映射和素材重新链接也没验证”；“`|| true` 会吞掉真正的错误，我应该更谨慎”。

**T4-opus-1**
- C59 [T4-opus-1] 先 `command -v ffmpeg ffprobe auto-editor python3 uv` 并检查 numpy；之后 `ls -d /Applications/Final\ Cut\ Pro*.app /Applications/DaVinci\ Resolve*`，发现都没装，因此“没有官方 DTD 可做严格校验”。
- C60 [T4-opus-1] 导出 FCPXML 1.8 主文件 + CMX3600 EDL 备份 + CSV 剪点表，每个片段引用完整源文件，“被剪掉的停顿还在素材里，可以在软件里拖回”。
- C61 [T4-opus-1] 按全局规则 `uv venv` 并 `uv pip install pytest --offline`（离线缓存），写了 8 个 pytest；测试失败时 subprocess 堆栈很长，有用的只有最后一行 `Unable to parse "end" option value "143/15"`；它据此把时间写法从 `143/15s` 改成 `286/30s`，理由“FCP 惯用的帧时长分母，避免某些导入器挑剔”（没有验证）。
- C62 [T4-opus-1] debrief：`<format>` 没写 name、asset 用 `src` 而不是 `media-rep`、`<!DOCTYPE fcpxml>` 的写法“都是我按印象定的，没有官方 DTD 也没有真软件验证过”。

**T4-opus-2**
- C63 [T4-opus-2] 从 cwd 路径里看到 “autocut”，`command -v ffmpeg ffprobe autocut auto-editor` 并 `ls ~/Codes/autocut | head`；没发现可用的 `autocut` 命令，继续用 ffmpeg。
- C64 [T4-opus-2] -45dB 的 silencedetect 一个结果都没有，与自己的 RMS 表（停顿段约 -50dB）对不上，它用“silencedetect 按采样峰值判断”解释，没有验证，改选 -35dB。debrief 承认“没去验证这个假设”。
- C65 [T4-opus-2] 导出 FCPXML 1.9 + EDL + CSV，另写了 8 个 unittest；把按剪辑表重组的音频 `_check_audio.wav` 留在工作目录供试听；抽帧对照图里 3 处晚了 1 帧，归因于“抽帧时四舍五入”，没有用精确帧号重抽就写进了最终回复。
- C66 [T4-opus-2] 最终回复给了 FCP“文件→导入→XML”、Resolve“文件→导入→时间线”的操作步骤，同时 debrief 说 format 没写 name、用了 1.9 版的 media-rep、Resolve 对 1.9 和片段 marker 的支持都是凭规范记忆。

**跨 case**
- C67 [全部 16 个] debrief 都承认“没有听过音频”；没有任何一个 agent 拿成片音频和源音频做过逐段比对（互相关/波形）；复核手段只有：重跑同一个检测器（silencedetect / RMS）、重转写、时长与帧数、抽帧。
- C68 [全部 16 个] 把“音视频流时长相等”当作音画同步的检查（T1-opus-1 “33.467s 一帧不差”，T2-haiku-1 “23.600 / 23.598”，T3-opus-1 视频 32.700 / 音频 32.707，T3-opus-2 “视频流比音频短 1 帧”）；没有 agent 检查口型/事件级同步。
- C69 [T2-haiku-1][T2-haiku-2][T2-opus-2] init 事件里这三个 case 的可用工具数是 26，其余 13 个是 107：同一套配置下，agent 看到的工具集不稳定。
- C70 [T1-opus-1][T1-opus-2][T2-opus-2][T4-opus-1][T4-opus-2] 用户全局的 Python 规范（logging、类型注解、`uv venv`、pytest）进入了剪辑任务：这些 case 比同任务的 haiku 多出补注解、建 venv、写测试的调用（T4-opus-1 19 次调用、$1.18）。
- C71 [T1-opus-1][T4-opus-2] 运行 uv 时看到 `[uv-shim] network=expensive -> offline`，把它当成“确认了没走网络”的证据；debrief 称“无关噪音”。
- C72 [T2 ×4] 要求汇报“一共剪掉多少时间”：4 个 case 报的数字都与实测一致（±0.02s）；其中 opus-2 报的是计划值，自己在 debrief 里指出了和成片差 0.02s。
- C73 [T3-haiku-1][T3-haiku-2] 两个 haiku 都在同一件事上失败：相信 whisper 时间戳 → 切错位置 → 用同一个 whisper 复核看到“按” → 解释为无害 → 声称成功；两个 opus 都在 whisper 时间戳与静音区间对不上时放弃了时间戳、改用能量/静音找“嗯”。
- C74 [T3 ×4] 四个 case 都能找到 `whisper-cli` 和一个 ggml 模型（均在仓库 `models/` 下），都把“本机已有”当作可用，没有一个考虑过这个模型文件“不是为这个任务准备的”。

---

# 第二章 批 2：各视频类型（T5–T9）

## 2.1 方法

沿用第一章的模型、命令、debrief 原文；差异如下。

**runset**：`baseline2-2026-10-28`；5 个任务 × opus/haiku × 2 次 = 20 个 case；`--max-turns 80`、`timeout 1200`（`tasks.json` 里每个任务单独设置）；`xargs -P 4`。

**素材**（`fixtures/media/`，都带 `*.truth.json`；沙盒里按任务复制对应素材并保持子目录结构，**不放 truth**，弹幕 jsonl 属于输入所以放）
- T5 `game_commentary.mp4`：640×360，37.6s，三条音轨（a:0 mix / a:1 mic / a:2 game）；truth 里 gap 带 tag boring / action，events 是 3 个游戏内爆炸。
- T6 `stream_vod.mp4`（150s）+ `stream_vod.chat.jsonl`：truth 有 3 个 highlight（主播的惊呼，各 3.9–4.8s）和 1 个诱饵（59.5–70s 抽奖刷屏）。
- T7 `montage/`：clip1–6.mp4（各 4s，肉眼可分的 lavfi 图案，自带音轨）+ `music_120bpm.wav`（24s，8s 循环）。
- T8 `knowledge/`：script.md（6 句）+ img1–3.png（1280×720，纯色底 + 白字）；truth 给了参考配音时长 32.9s。
- T9 `ytpmv/`：source_clip.mp4（6.7s，两句台词）+ `music_120bpm.wav`（16s）；truth 逐字给出 12 个音节的区间。

**任务 prompt 原文**（统一后缀、debrief 与第一章相同）
- T5 game_commentary：`game_commentary.mp4 是我用 OBS 录的游戏实况（有好几条音轨），帮我把我没说话、也没啥看点的无聊段落剪掉，打怪那种虽然没说话但很精彩的地方要留着。输出 game_cut.mp4。`
- T6 stream_clips：`stream_vod.mp4 是我的直播录像，stream_vod.chat.jsonl 是弹幕记录。帮我切 3 个高光片段发抖音：每个 15–30 秒，竖屏 9:16，带字幕。输出 clip_1.mp4、clip_2.mp4、clip_3.mp4。`
- T7 montage：`montage 文件夹里有 6 段素材和一首 BGM（music_120bpm.wav），帮我做一个 15 秒左右的卡点混剪，画面切换要踩在鼓点上。输出 montage.mp4。`
- T8 knowledge：`根据 knowledge/script.md 做一个知识科普短视频：要有配音、字幕和配图（图在 knowledge 文件夹里），横屏。输出 knowledge.mp4。`
- T9 ytpmv：`用 ytpmv/source_clip.mp4 里的台词做一个鬼畜：跟着 ytpmv/music_120bpm.wav 的节奏，把字切开重复、卡在拍子上。输出 ytpmv.mp4。`

**隔离（批 2 新增）**：被测 agent 通过 `sandbox-exec`（macOS Seatbelt）运行，规则 `deny file-read-data`：仓库、scratchpad 所在 session 目录（含其他 case 的沙盒、校验用的 calib）、`~/.claude/projects/-Users-lishuyu-Codes-autocut`、`~/.claude/teams`；只放行本 case 沙盒。只禁读内容/列目录、保留 stat，否则 cwd 里的 `ls -la` 会因 stat 不了父目录而失败（批 2 前两次启动正是这个原因作废，见下）。已知没隔离掉的：① cwd 路径里的 “autocut” 字样；② 并发 case 共用的 `/tmp`（见卡片）；③ 本机装的 whisper-cli、brew 里的东西、`~/.cache` 里的模型——这些是用户机器的真实环境，保留。批跑外面包了 `caffeinate`，`timeout` 带 `-k 20`。
- 作废的启动（证据留在 `evals/runs/aborted-*`，只当旁证）：①`aborted-baseline2-noniso-sleep`：未隔离 + 主机睡眠导致耗时失真（T5-opus-1 记 5624s 但 `timeout 1200` 没触发）；②`aborted-baseline2-iso-lsdenied`：隔离规则用了 `file-read*`，每个 case 的首条 `ls -la` 都因 `..` 被拒而报 Operation not permitted。

**校验器** `evals/verify_genres.py`（复用 `verify.py` 的互相关函数；32 个测试 `tests/test_verify_genres.py`，每个任务至少一个应 pass 的理想产物和一个应 fail 的产物）：
- T5：a:0 混音里所有 speech intact 且不被削；3 个爆炸事件都在产物里；每个 boring 间隔残留 ≤1.0s（+1 帧容差）。残留用“锚点法”度量（intact 语音和在场事件做锚点，残留 = 相邻锚点在产物里的间距 − 源里除该间隔外的间距）——因为游戏音轨是周期约 1.29s 的循环音乐，逐窗口匹配会处处误报“存在”。
- T6：恰好 clip_1..3.mp4（无多余 `clip_N.mp4`）；每个 15–30s、9:16（±0.01）、有音频；取开头/中段/结尾 3s 音频回源里定位——直播背景音是周期循环的（尾部窗口会在 72s 之后同分重现），所以每个窗口取全部候选位置再求三者一致的偏移；连续区间要完整覆盖某个 highlight（±0.25s），3 个切片对应 3 个不同 highlight。字幕有字幕流或同名 .srt/.ass/.vtt 记 verified，否则 `burned_or_missing`（只记录）。
- T7：12–18s；产物音频与 BGM 互相关 ≥0.6（BGM 是 8s 循环，偏移只能确定到循环周期，但周期是拍长的整数倍，不影响对拍判定）；scene>0.3 的画面切点 ≥4 个，落在拍点/半拍 ±34ms 内的比例 ≥80%。
- T8：横屏；时长在参考配音时长的 0.7–1.6 倍；非静音占比 ≥50%；配图 ≥2 张出现。**与任务书的差异**：配图匹配用 RGB 64×36 而不是灰度——img2（青蓝）和 img3（橙）转灰度后亮度几乎相同，分不开；字幕有字幕流/侧车文件且与脚本字符重合 ≥60% 才算 verified，其余只记录。
- T9：产物含 BGM（互相关 ≥0.5）；truth 音节在产物里的出现 ≥6 次（互相关 ≥0.6，非极大值抑制）；落在拍点/半拍 ±50ms 内的比例 ≥70%。**补充一条**：必须有音节被重复（出现次数 > 不同音节数）——把原片放一遍也有 12 次出现，规格里只写“≥6 次”会放过。
- 所有产物另记 `loudness`（ebur128 积分响度/真峰值），供“成片质量”栏参考。
