# Baseline 观察：没有 autocut 时，agent 怎么剪视频

阶段：Empathize · 日期：2026-10-28 · 范围：只观察，不聚类，不提方案（Define 阶段再做）。
原始证据：`evals/runs/<runset>/*.jsonl`（stream-json 全文，gitignored，只在磁盘）、`*-debrief.jsonl`、`*-verify.json`、`metrics.md`。
读法：卡片都从 agent 的视角写它**相信了什么 / 看到了什么**；`(suggestion)` 是 agent 自己在 debrief 里提的建议，只当假设。卡片编号只是引用用：A = 批 1 隔离版，B = 批 2，U = 未隔离的批 1，L = 泄露与基础设施事件；每张卡带 run ID。

| 章 | 内容 | runset | 环境 | 结果 |
|---|---|---|---|---|
| 二 | 批 1：口播四个任务 T1–T4 | `baseline-2026-10-28-iso` | iso | haiku 8/8，opus 8/8（51 张卡片；T4-haiku-2 是重跑） |
| 三 | 批 2：各视频类型 T5–T9 | `baseline2-2026-10-28` | iso（6 个重跑为 iso+信号隔离+/tmp 清理） | haiku 5/10，opus 9/10（65 张卡片；T6-haiku-2、T8-haiku-2 是重跑） |
| 四 | 未隔离的批 1 + 泄露/污染/基础设施事件 | `baseline-2026-10-28`、`aborted-*`、`contaminated/` | noniso / 作废 | 未隔离 haiku 6/8，opus 8/8（74 + 27 张卡片） |

---

# 第一章 方法与环境（共用）

## 1.1 共用方法（两批都适用）

**环境**
- macOS arm64；ffmpeg/ffprobe 9.0.2；`claude` CLI 2.1.303；被测模型 `--model opus`（解析为 claude-opus-5-5）和 `--model haiku`（claude-haiku-5-5）。
- 素材：`fixtures/media/talk_zh.mp4`（中文 TTS，40.7s，底噪 -52dBFS）、`lecture_en.mp4`（英文，35.3s，-38dBFS 粉噪），旁边各有 `*.truth.json`（每段 speech/gap 的精确区间，tag = filler / retake）。生成脚本 `fixtures/make_fixtures.py`。
- 被测 agent 的 cwd 是一次性沙盒 `…/scratchpad/evalwork/<runset>/<task>-<model>-<n>/`，只放一份 mp4 副本，不放 truth.json。
- 被测 agent 继承用户的全局 Claude Code 配置（这是真实使用场景，不是为 autocut 准备的）：init 事件显示 71 个 skill、14 个 MCP server、107 个工具（另有若干 case 只有 26 个，见卡片），首轮上下文约 41k token；全局 CLAUDE.md 与 rules 会加载（后面有卡片显示 Python 规范渗进了剪辑任务）；`CLAUDE_EFFORT=xhigh` 从环境继承。没有任何视频相关 skill。
- 启动脚本清掉了父会话身份环境变量（`CLAUDECODE`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_MESSAGING_*`、`CLAUDE_CODE_BRIDGE_SESSION_ID`、`CLAUDE_CODE_PLUGIN_DIRS` 等），避免被测 agent 连上当前会话的消息通道。

**隔离级别**：本文的证据来自三种运行环境，`metrics.md` 里每个 case 有“环境”列（iso / noniso）。(a) **noniso**：沙盒只是 cwd 在仓库外，文件系统不隔离——最早的批 1（第四章，只作补充证据）；(b) **iso**：被测 agent 在 macOS Seatbelt（`sandbox-exec`）里跑，`deny file-read-data` 仓库、scratchpad 里其余 case 的目录、本项目 `~/.claude` 记忆/teams，只放行本 case 的沙盒（第二章的批 1 隔离版、第三章的批 2）；(c) **iso + 信号隔离 + `/tmp` 残留清理**：在 (b) 之上加 `(deny signal)`、素材复制排除隐藏文件、每个 case 结束后移走它用过的 `/tmp` 顶层路径（只有第三章批 2 里被剔除后重跑的 6 个 case 是在这个环境里跑的）。**cwd 路径里的 “autocut” 字样三种环境都没处理**（沙盒必须在 scratchpad 下），fixture 画面左下角烧录的 “autocut fixture …” 文字也在，agent 读得到。

**命令**（`evals/run_headless.sh`；批跑入口 `evals/run_matrix.sh`，`xargs -P 4`）
```
cd <沙盒> && timeout 900 claude -p "<prompt>" --model <opus|haiku> --output-format stream-json --verbose \
    --dangerously-skip-permissions --max-turns 60 < /dev/null > evals/runs/<runset>/<case>.jsonl
claude -p "<debrief>" --resume <session_id> --output-format stream-json --verbose \
    --dangerously-skip-permissions --max-turns 3 > evals/runs/<runset>/<case>-debrief.jsonl
```
每个 runset：任务 × 2 个模型 × 2 次；原始 transcript 先落盘再分析。批 1 隔离版 16 个 case，批 2 20 个 case（T5–T9 的 prompt、素材和校验器见第三章）。

**任务 prompt 原文**（`evals/baseline/tasks.json`；每个末尾统一追加后缀）
- T1 long_pauses：`talk_zh.mp4 是我录的一段口播，帮我把说话中间那些长时间的停顿剪掉，正常的短停顿留着，别剪得太碎。输出成 talk_zh_cut.mp4。`
- T2 noisy_silence：`lecture_en.mp4 是在比较吵的教室里录的课，帮我把没人说话的部分去掉，输出 lecture_en_cut.mp4，顺便告诉我一共剪掉了多少时间。`
- T3 retake_filler：`talk_zh.mp4 里有一处我说错了又重新说的地方，还有个“嗯”，帮我把说错的那段和口头禅剪掉，输出 talk_zh_clean.mp4。`
- T4 export_nle：`帮我把 talk_zh.mp4 粗剪一下（去掉长停顿），但先别渲染成片，导出一个 Final Cut Pro 或 DaVinci Resolve 能打开的工程/时间线文件，我要自己再微调。`
- 统一后缀：`（工作时请简要说明你选每一步的理由；有任何拿不准的地方也顺手说出来。注意：当前网络按流量计费，不要下载模型、不要安装新的软件包。）`
- debrief 原文：`刚才这个任务结束了，请简短回答，每条 1–2 句：1) 哪一步你最没把握，为什么？2) 你是怎么确认输出是对的？哪些地方没有确认？3) 过程中哪些命令的输出让你困惑、太长或者没用（如果有）？`

**校验器**（`evals/verify.py`，27 个测试 `tests/test_verify.py` 全过）：把源和产物音频解成 16kHz mono、300–3400Hz 带通，对 truth 里每段 speech 在产物里做归一化互相关；≥0.8 intact，<absent 阈值 absent，之间 partial；absent 阈值 = max(0.3, 该模板在源里排除自身落点后的最高误匹配分数 + 0.03)（短口头禅“嗯”会误匹配到 0.5 左右，固定 0.3 会把剪干净的产物判成 partial——实测标定后改的）。另算 coverage（intact 但头尾被削记 clipped）、相邻存活片段间隔（>1.0s 死寂、<0.08s 挤在一起）、音画时长差 ≤0.15s。T4 解析 FCPXML / xmeml / EDL / OTIO，查源文件名引用、总时长在 [无 tag 语音总长, 原始时长-1.0s]，另加两项：该保留的语音在时间线源区间里被覆盖 ≥90%、长停顿在时间线里保留 ≤1.0s。

---

# 第二章 批 1（隔离版）：口播类四个任务

runset `baseline-2026-10-28-iso`；T1–T4 的 prompt 原文、模型、命令、校验器与第一章相同；**被测 agent 在 Seatbelt 读隔离下运行**（`deny file-read-data`：仓库、scratchpad 其余目录、本项目 `~/.claude` 记忆/teams；这一批没有信号隔离，也没有 `/tmp` 残留清理，见第四章）。批跑顺序为“第几次 → 模型 → 任务”交错，`-P 2`。16 个 case，原始 transcript 在 `evals/runs/baseline-2026-10-28-iso/*.jsonl`，指标在同目录 `metrics.md`（新增“环境”列：iso / noniso）。

## 2.1 结果总览

| task | model | pass | 平均调用 | 平均 $ | 说明 |
|---|---|---|---|---|---|
| T1 long_pauses | haiku | 2/2 | 4.0 | 0.01 | |
| T1 long_pauses | opus | 2/2 | 8.5 | 0.62 | |
| T2 noisy_silence | haiku | 2/2 | 7.5 | 0.02 | 剪掉 9.1s / 11.6s |
| T2 noisy_silence | opus | 2/2 | 14.5 | 0.90 | 剪掉 11.0s / 11.8s |
| T3 retake_filler | haiku | **2/2** | 24.0 | 0.04 | 未隔离版是 0/2；见卡片 A1–A17 |
| T3 retake_filler | opus | 2/2 | 13.0 | 0.73 | |
| T4 export_nle | haiku | 2/2 | 8.0 | 0.02 | 都没能在 FCP/Resolve 里打开验证；haiku-2 是重跑（原件被外部 pkill 波及，见 4.4） |
| T4 export_nle | opus | 2/2 | 21.0 | 1.22 | 同上 |
| 合计 | haiku | 8/8 | 10.9 | 0.02 | 总 $0.17 |
| 合计 | opus | 8/8 | 14.2 | 0.87 | 总 $6.95 |

注：T3-haiku-2 的校验在 21:50:16 崩过一次（`verify_error`，与外部会话的批量 `pkill` 同一时刻，无法复现），已用 `evals/verify_case.sh` 对留存的沙盒重新校验，结果为 PASS，其余 case 的校验不受影响。T4-haiku-2 的 agent 进程也在那一刻被波及，已按要求归档原件（`interrupted/`）并重跑，上表和下文用的是重跑结果（见 4.4）。「自验」列已按 transcript 人工核对：16 个 case 全部复核过成片，手段同质（重跑同一检测器 / 重转写 / 时长与帧数 / 抽帧），见卡片。T3-haiku-1 的耗时栏（14s）来自 result 事件的 `duration_ms`，该 case 用了后台任务和 Monitor，wall 时间以 `*-meta.json` 为准。

与未隔离版（第四章）的差异：**T3-haiku 从 0/2 变成 2/2**，原因不是 agent 变聪明，而是模型和 ASR 来源变了：未隔离时两个 haiku 都用仓库里的 whisper.cpp ggml 模型，词级时间戳把“嗯”标早了约 1.7s；隔离后读不到那个模型，两个 haiku 各自找到了别的 ASR 路线（macOS Speech 框架 + 声学特征；借用另一个项目 venv 里的 openai-whisper），时间戳准确。

## 2.2 逐 case 轨迹摘要

**T1-haiku-1**（PASS · 4 调用 · $0.01）-35dB silencedetect 一次，看到停顿“0.8 与 1.2 之间有明显空档”，以 1.0s 为界，≥1s 压到约 0.4s（两侧各 0.2s）；写 shell 脚本、一条 trim/concat 渲染、对成片重跑 silencedetect（最长剩余 0.82s）。debrief：1.0s 阈值没有依据用户意图校准，只试了一个阈值。

**T1-haiku-2**（PASS · 4 调用 · $0.01）同样路径，≥1s 两侧各留 0.25s；输出 33.33s 与预期一致，复测最长静音 0.82s。

**T1-opus-1**（PASS · 11 调用 · $0.65）测音量分布→-40dB；判 32.5–33.3s 为呼吸并整段剪；dry-run 后渲染；抽接缝帧拼图并 Read、对烧录时间码；手打路径少了一个 `/`，`cd` 报错但后续命令碰巧成功。

**T1-opus-2**（PASS · 6 调用 · $0.59）测分布→-40dB，1.0s 门槛、压到 0.5s；读到全局规则后把脚本改写成 logging+类型注解并 `uv venv`；“有声时长 26.71→26.70s”证明没吃字。

**T2-haiku-1**（PASS · 9 调用 · $0.02）silencedetect 的 grep 无匹配导致整条命令退出 1；改用标准库算窗口响度→-30dB；自己发现加缓冲后相邻段间留下 0.3s 小缝并合并；剪掉 9.14s。

**T2-haiku-2**（PASS · 6 调用 · $0.02）100ms 响度双峰→-28dB；`-filter_complex_script` 不被这个 ffmpeg 支持，改直传；剪掉 11.64s。

**T2-opus-1**（PASS · 9 调用 · $0.90）带通后发现中间电平，用过零率/峰值因子、基频自相关区分人声与冲击声，排除一个像提示音的段落，查高频确定边界留 0.2s；视频少 2 帧→半帧容差重渲；剪掉 11.02s。

**T2-opus-2**（PASS · 20 调用 · $0.90）带通后 `silencedetect` 无输出，不深究，改自写 20ms RMS 分段；找 ggml 模型无果，用自相关浊音率判断结尾 0.86s 是人声并保留；zsh 不拆分 `$seg`；ffmpeg 9 的 `-/filter_complex <文件>` 新写法；剪掉 11.81s。

**T3-haiku-1**（PASS · 33 调用 · $0.05）探测 ASR：`.pt` 模型 + 没有 torch/mlx/whisper 运行时；`ls /Users/lishuyu/Codes/autocut` 被拒（“我不去绕过它”）；改写 Swift 调 macOS Speech 框架（本地识别），三版才跑通（只回结尾 5s → 主线程 semaphore 堵死回调 → RunLoop）；Speech 不输出“嗯”，用能量+过零率+低频占比定位 14.40–14.80；用同一 Speech 对成片重转写验证。

**T3-haiku-2**（PASS · 15 调用 · $0.02）全盘 `find` 太慢、TaskStop 停掉；找到 `/Users/lishuyu/Codes/autobake/.venv` 里有 whisper、缓存里有 `large-v3-turbo.pt`，借用跑词级转写（嗯 14.40–14.66，准确）；静音检测对齐切点；用同模型对成片重转写。

**T3-opus-1**（PASS · 13 调用 · $0.75）判定 `.pt` 不能给 whisper-cli 用，选 macOS SpeechTranscriber（zh-CN 本地模型，带 audioTimeRange），预先想到“可能吞嗯，需静音/能量交叉验证”；ASR 标“嗯”13.92–14.70 与静音区间对不上，凭“空隙里唯一的声音”推断；成片重转写（转写里有“路频”错字）。

**T3-opus-2**（PASS · 13 调用 · $0.72）借用 `~/Codes/classnote/.venv` 的 mlx_whisper + 本地缓存的 whisper-large-v3-mlx（turbo 权重不全），`HF_HUB_OFFLINE=1`，带语气词的 initial_prompt；用能量包络修正 whisper 的 ±0.2s 误差（嗯 14.49–14.88，重说切点 27.89 而非 26.92）；抽帧核对时间码；成片重转写。

**T4-haiku-1**（PASS · 11 调用 · $0.02）想“看 autocut 项目里有没有现成时间线导出代码”（被拒后自己写）；导出 FCPXML 1.9 + CSV；第一次“验证通过”是假的（grep 吞了 ffmpeg 的 234 错误，`|| echo` 当成成功），自己起疑后重做。

**T4-haiku-2**（PASS · 5 调用 · $0.01 · 重跑；原件 14 调用/$0.03 见 4.4）`silencedetect` 找到 4 段 ≥1s 停顿，每段两端各留 0.25s、结尾只留 0.25s，一个脚本生成 FCPXML 1.9，Python 解析 + 内存拼接复测（最长剩余 0.82s）；debrief：`media-rep` 路径、`audioRole` 两边能否接受不确定。原件（interrupted）的路径更长：`find /` 转后台后 TaskStop，同时导出 FCPXML 1.10 + xmeml v5。

**T4-opus-1**（PASS · 27 调用 · $1.47 · 全批最贵）先 `ls ~/.claude/projects/…`；`cd` 路径拼错白跑一轮；导出 FCPXML 1.10 + EDL + CSV；用 uv venv + brew pytest 写 4 项反向解析测试，接缝帧像素比对；验证脚本自己的舍入错误让接缝拼图“左右两帧连续”，查了一轮。

**T4-opus-2**（PASS · 15 调用 · $0.97）权衡“全局规则要求 venv”与“不许装包”，选 brew python3 + pytest；导出 FCPXML 1.9 + EDL + CSV；写单测时发现“文件以静音结尾且无 silence_end → floor(inf)”的潜在 bug 并修；都没能在 NLE 里验证。

## 2.3 原始观察卡片（隔离版批 1）

> 编号只是引用用，按 case 顺序排，不是分类。

**T3（四个 case 的 ASR 来源）**
- A1 [T3-haiku-1] 第一条命令连着确认本机 ASR：`command -v whisper whisper-cli whisper-cpp mlx_whisper vosk`；它看到 `~/.cache/whisper` 里有 `base.pt`/`large-v3-turbo.pt`，但 `whisper-cli` 要 ggml 格式，于是认为“没有现成 ggml 模型，`.pt` 不装包就加载不了”。
- A2 [T3-haiku-1] 想起“项目名是 autocut，它的 venv 里可能已装了 whisper”，`ls /Users/lishuyu/Codes/autocut` 返回 Operation not permitted；它写“项目 venv 因沙箱权限无法读取，我不去绕过它”，转去查 HF 缓存。
- A3 [T3-haiku-1] 确认没有任何可运行的 ASR 运行时（torch/mlx/whisper 都没有）后，想到 macOS 自带 Speech 框架，写 Swift（`SFSpeechRecognizer`，`requiresOnDeviceRecognition = true`，理由“音频不出本机、不走流量”）。
- A4 [T3-haiku-1] Speech 脚本三版才跑通：第一版只返回结尾约 5s（前 35s 没输出），它先怀疑音频，量了响度才判断是识别端；第二版在主线程 `semaphore.wait`，把回调堵死（`callbacks=0`）；第三版改 RunLoop。过程中用 ToolSearch 加载 Monitor 等后台结果。全程 33 个调用。
- A5 [T3-haiku-1] 中间 partial 回调的时间戳全是 0，只有 4 个回调带真实时间；debrief：“每次只看 `callbacks[-1]` 文字都不完整，直到按重置边界拼接才看到全文”。
- A6 [T3-haiku-1] Speech 不输出“嗯”，它找“没有文字覆盖的语音间隙”：14.45–14.75s 过零率低（约 1340Hz）、低频能量占比 0.71，判为浊音哼声=“嗯”，排除 19.3s 的气声；定位正确（14.40–14.80）。最终回复：“‘嗯’的判定是声学推断，没有文字证据”。
- A7 [T3-haiku-2] `find ~ /opt/homebrew /usr/local -iname …` 全盘找模型，120s+ 没结果，转入后台后用 TaskStop 停掉；debrief：“浪费了时间”。
- A8 [T3-haiku-2] 发现 `/Users/lishuyu/Codes/autobake/.venv/bin/python`（另一个项目的 venv）里装了 `whisper`，`~/.cache/whisper/large-v3-turbo.pt` 在缓存里，就“借用”它做词级转写（只读、不装包）；时间戳准（“嗯”14.40–14.66），一次就剪对，并用同一模型对成片重转写确认。
- A9 [T3-opus-1] 判断“HF 缓存里的 mlx 模型只是空壳、`.pt` 要 openai-whisper+torch、`whisper-cli` 自带的只是不能识别中文的测试 tiny 模型”，选 macOS SpeechTranscriber（zh-CN 本地模型，每段文本带 audioTimeRange），并预先想到“系统转写可能吞掉‘嗯’，要结合静音/能量交叉验证”。
- A10 [T3-opus-1] ASR 把“嗯”标在 13.92–14.70s，与静音区间对不上；它凭“14.51–14.73 是那段空隙里唯一的声音”推断是“嗯”，没有单独再识别一次确认（debrief）。成片转写里有识别错误“一段路频”，最终回复仍把这份转写当作“内容一个字没少”的证据。
- A11 [T3-opus-1] debrief：每条命令后提示 “Shell cwd was reset”，用 `$OLDPWD` 拼路径不稳，改成绝对路径；`freezedetect` 什么都没输出，分不清“画面没静止”还是“命令没生效”。
- A12 [T3-opus-2] 在 `~/Codes/classnote/.venv` 里找到 `mlx_whisper`，借用其解释器；turbo 权重没下全，改用完整的 `whisper-large-v3-mlx`，设 `HF_HUB_OFFLINE=1` 防联网，并加带语气词的 `initial_prompt` 防止吞“嗯”。
- A13 [T3-opus-2] whisper 时间戳有 ±0.2s 误差，用能量包络修正：“嗯”实为 14.49–14.88，说错重说的切点应在 27.89 而非 whisper 标的 26.92。
- A14 [T3-opus-2] 场景检测命令什么都没输出，分不清“画面没切换”还是“命令写错”，靠看抽帧确认（debrief）。
- A15 [T3 ×4] 四个 case 对成片的验证都是“用同一个 ASR 再转写一遍”；T3-haiku-1 还补了能量判断。没有 agent 比较成片音频与源音频的逐段一致性。
- A16 [T3 ×4] 四个 case 的 ASR 来源互不相同（Speech 框架 ×2、openai-whisper@autobake、mlx_whisper@classnote），都是 agent 在用户主目录里自己找到的；没有一个在任务书里被提示过。
- A17 [T3-haiku-1] 33 个调用里，探测本机 ASR 和调试 Speech 框架回调占了绝大多数；定切点、渲染、验证加起来只有最后 10 个左右。同任务的 T3-haiku-2（借用现成 whisper 环境）只用 15 个调用。

**T4**
- A18 [T4-haiku-1] 从 cwd 路径读到 “autocut”，想“看 autocut 项目里有没有现成的时间线导出代码可以复用”；`ls` 被拒后写“没有现成的 autocut 代码可复用，我自己写”。
- A19 [T4-haiku-1] 验证命令 `… | grep … || echo "验证通过"`：ffmpeg 报错（exit 234）、grep 没匹配、`|| echo` 把它当成成功，打印了“通过”；它自己起疑（“连 Duration 都没匹配到”），重跑后发现是 shell 解析 CSV 时变量被弄乱（`0.000` 变 `000`），改用 Python 拼 filtergraph，又因 `-af` 与 `-filter_complex` 同用冲突才修好。debrief：“第一次的‘验证通过’是假的”。
- A20 [T4-haiku-2（interrupted 原件）] 同时导出 FCPXML 1.10（给 FCP）和 xmeml v5（给 Resolve）两份；用 `file://localhost` 绝对路径；debrief 担心“FCP/Resolve 会不会接受绝对路径、轨道数和音频声道设置”。
- A21 [T4-haiku-2（interrupted 原件）] 自己看剪辑表发现 `14.299→14.311` 这种 12ms 的剪切“只会制造跳帧”，改成“只有能省下 ≥0.2s 才剪”。
- A22 [T4-haiku-2（interrupted 原件）] `find / -name talk_zh.mp4` 太慢转入后台，用 TaskStop 停掉；一次命令返回 12,291 字符的 50ms RMS。
- A23 [T4-opus-1] 第一条命令就 `ls ~/.claude/projects/-private-tmp-claude-501--Users-lishuyu-Codes-auto…`（看自己 cwd 对应的 claude 项目目录，想找记忆/先前上下文）；随后 `cd` 路径拼错（连字符形式 `autocut-457…`），第一批检查输出全是错误，debrief：“白跑了一轮”。
- A24 [T4-opus-1] 全局 Python 规则进入任务：`uv venv --offline`、补类型注解、用 brew 的 pytest 写 4 项“从导出文件反向解析”的测试（帧对齐与总长、EDL 与 FCPXML 逐帧一致、拼接音频无长停顿且语音总长不变、接缝帧像素比对）。27 个调用、$1.47，是这批最贵的 case。
- A25 [T4-opus-1] 接缝拼图显示左右两帧连续、没有跳变，它困惑了一轮，最后查出是验证脚本用了四舍五入的秒数 trim、接缝帧号又心算错，不是导出有问题（debrief）。
- A26 [T4-opus-2] 明确权衡全局规则和任务约束：“按你的 Python 规则本该先建 venv，但建了也得装 pytest，而这次不允许装包，所以直接用 brew 的 python3 和 pytest 跑”。
- A27 [T4-opus-2] 给自己的脚本补单测时发现真 bug：文件以静音结尾且 ffmpeg 没输出 `silence_end` 时 `floor(inf)` 溢出；这次素材恰好有 `silence_end` 才没触发；它修复并加了测试。
- A28 [T4-opus-1][T4-opus-2] zsh 把 `$F[0:v]`/`$F[0:a]` 当数组下标，报 `bad math expression`，错误信息太短，各浪费一轮（debrief）。
- A29 [T4-opus-1][T4-opus-2][T4-haiku-1][T4-haiku-2（原件与重跑）] 五个都把“能否在 NLE 里导入”列为最没把握；opus 两个查了 `/Applications` 里没有 FCP/Resolve，`<format>` 没写 `name`；验证的对象都是“自己导出文件的内部一致性”（xmllint、内存拼接复测、单测）。

**T1 / T2**
- A30 [T1-haiku-1] 直接 -35dB silencedetect，看到“0.8 与 1.2 之间有明显空档”，以 1.0s 为界、≥1s 压到约 0.4s；debrief：“1.0 秒阈值没有依据用户的意图校准”“只用了一个 -35dB，没有测试其他阈值”。
- A31 [T1-haiku-2] debrief：concat 编码输出带大量 x264 参数和统计，“对判断没有帮助，我只看了最后的 duration 和 silencedetect 行”。
- A32 [T1-opus-1] -45dB 那次 silencedetect 什么都没输出，“后来才想明白是底噪瞬时峰值超过了 -45dB”；第一条命令 `cd` 报目录不存在，但后面的 ffprobe 却成功（手打路径少了一个 `/`，shell 本来就在目标目录，“碰巧没出事”）；抽帧用的帧号是心算的，第二个接缝两帧没对准，靠画面烧录时间码核对才确认没问题，“这一步做得不够严谨”。
- A33 [T1-opus-2] 读到全局规则后“项目规则要求 Python 代码用 venv、logging 和类型注解，`uv venv` 只是用本机解释器建环境、不联网，符合‘不下载’的约束”，把脚本改写成合规形式。
- A34 [T1-opus-2] silencedetect 把 32.77s 的一段连续停顿拆成两段，成品里又把一个 0.51s 的停顿拆成 0.19+0.31，“两次都要人工核对才能确认不是问题”；用“有声时长 26.71→26.70s”证明没切到说话。
- A35 [T2-haiku-1] `grep` 没匹配到 silencedetect 输出→`pipefail` 让整条命令返回 1；它判断“-35dB 下没检出静音，符合教室底噪高”；debrief：“花了一步才判断这是阈值太低造成的、不是命令出错”。选 -30dB（底噪 -41、说话 -14~-24）；发现加 0.3s 缓冲后相邻段之间留下 0.3s 小缝，改为加缓冲后再合并。
- A36 [T2-haiku-2] 100ms 响度双峰（-13~-23 说话 / -36~-42 底噪）→选 -28dB；`-filter_complex_script` 在这个 ffmpeg 里不支持，改直传；debrief：逐窗口输出全打印，“只用到了分布形状”。
- A37 [T2-opus-1] 带通后看到 -30~-38dB 的中间电平，用 50ms 过零率+峰值因子区分“音节起伏的语音”和“尖短脉冲的冲击声”，再自己实现基频自相关（0.88–0.99）判定“有基频的人声”；33.68–34.24s 那段比讲课声响 3dB、浊音度 0.99，怀疑是提示音，用基频轨迹排除，判为“可能是另一个人在说话”并保留；还查 >4kHz 高频，确认清辅音最多延伸到边界外 0.10s，所以每段留 0.2s。
- A38 [T2-opus-1] 视频帧数比预期少 2 帧（727 vs 729），原因是浮点误差让个别帧的时间戳落在切点上被 `between()` 排除，改成带半帧容差的半开区间重渲；用一处发声起点（预期 11.78s，实测 11.76s）核对音画同步。
- A39 [T2-opus-1] debrief：“复检用的是同一个检测器，它自己的误判（比如漏掉的轻声）在复检里也查不出来”；第一次按 0.5s 取平均，把说话和噪声混进同一格，算出一堆 -30~-38dB 的“中间电平”，“白查了一轮”；过零率那列全是 0.000——字段名在 ffmpeg 输出里不存在，脚本默认填 0，“当时也没发现它取错了”。
- A40 [T2-opus-2] 低频 <200Hz 的“鼓包”判为空调/车流类隆隆声，当静音处理；`silencedetect` 在这个 ffmpeg 9 里“没有按预期输出结果，我不深究原因了”，改用 20ms 窗口 RMS；zsh 不拆分 `$seg`；本机找 ggml 无果（HF 缓存只剩 refs、没有权重）后，用自相关浊音率（中位 0.89、浊音率 0.55）判断结尾 0.86s 的声音是人声并保留，debrief：“依据只是基频周期性这一个间接指标”。
- A41 [T2-opus-2] ffmpeg 9 去掉了 `-filter_complex_script`，报错里没提替代写法 `-/filter_complex <文件>`（debrief）；它自己查到并改用。
- A42 [T2 ×4] 汇报“剪掉多少时间”：haiku-1 9.14s、haiku-2 11.64s、opus-2 11.81s，与实测一致（±0.02s）；haiku 两个只比 opus 少剪 0–2.5s，都通过了 verify。

**跨 case**
- A43 [全部 16 个] init 事件里的可用工具数在 26、107、109、113、119、143、145、159、161 之间变化（同一套全局配置、同一个 harness）；T3-haiku-1 看到 161 个、T3-haiku-2 看到 26 个。
- A44 [T1-opus-2][T4-opus-1][T4-opus-2] 全局 CLAUDE.md/rules 的 Python 规范（venv、logging、类型注解、pytest）再次进入剪辑任务；T4-opus-2 把它和“不许装包”的任务约束做了显式权衡。
- A45 [全部 16 个] debrief 都承认“没有听过音频”；没有任何一个 agent 拿成片音频与源音频做逐段比对（互相关/波形）；复核手段是：重跑同一个检测器（silencedetect / RMS）、重转写、时长与帧数、抽帧；T2-opus-1 自己指出“复检用的是同一个检测器”。
- A46 [T1-opus-1][T4-opus-1] 都出现 `cd` 路径笔误（把 `…/autocut/457…` 写成 `…/autocut-457…` 或少一个 `/`）；T4-opus-1 的第一批检查输出因此全是错误。
- A47 [T2-opus-2][T3-opus-1][T4-opus-1][T4-opus-2] zsh 的变量分词/数组语法（`$seg`、`${!arr[@]}`、`$F[0:v]`）各浪费一轮；报错信息（`bad math expression`、`bad substitution`）都很短，agent 要靠猜。
- A48 [T2-haiku-2][T2-opus-2] 都撞上 ffmpeg 9 不再支持 `-filter_complex_script`；T2-opus-2 另外撞到 `silencedetect` 在某些阈值下无输出而选择绕开、没有查原因。
- A49 [T3-haiku-2][T4-haiku-2（interrupted 原件）] 都启动过全盘 `find`（`find ~ /opt/homebrew /usr/local …`、`find / -name talk_zh.mp4`），命令超时转入后台，需要 TaskStop 停掉；两个 debrief 都说这一步“浪费了时间/输出没有用上”。T2-opus-2（未隔离版）、T3-opus-1/2 也有类似的 `find ~ /opt/homebrew …`。
- A50 [T4-haiku-2（重跑）] 只导出一个 FCPXML 1.9（没有 xmeml），5 个调用、6 个 turn、$0.01；debrief：`media-rep` 路径、`audioRole` 等属性“能否被两边都接受，我并不确定”；“`silencedetect` 每条都带 `Parsed_silencedetect` 前缀，长而重复，我只需要起止时间”；第二次校验输出里 0.3s 以上的短停顿“看起来像是剩下的问题，容易误导” (suggestion：agent 自己提的改进想法，只当假设)。
- A51 [T4-haiku-2 原件 vs 重跑] 同一模型、同一任务，原件走“双格式导出（FCPXML 1.10 + xmeml）+ 14 次调用”，重跑走“单格式 + 5 次调用”；两次都通过校验，两次都没在 NLE 里打开过。

---

# 第三章 批 2：各视频类型（T5–T9）

## 3.1 方法

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

**隔离（批 2 新增）**：被测 agent 通过 `sandbox-exec`（macOS Seatbelt）运行，规则 `deny file-read-data`：仓库、scratchpad 所在 session 目录（含其他 case 的沙盒、校验用的 calib）、`~/.claude/projects/-Users-lishuyu-Codes-autocut`、`~/.claude/teams`；只放行本 case 沙盒。只禁读内容/列目录、保留 stat，否则 cwd 里的 `ls -la` 会因 stat 不了父目录而失败（批 2 前两次启动正是这个原因作废，见第四章）。已知没隔离掉的：① cwd 路径里的 “autocut” 字样；② 并发 case 共用的 `/tmp`（见卡片）；③ 本机装的 whisper-cli、brew 里的东西、`~/.cache` 里的模型——这些是用户机器的真实环境，保留。批跑外面包了 `caffeinate`，`timeout` 带 `-k 20`。
- 作废的启动（证据留在 `evals/runs/aborted-*`，只当旁证）：①`aborted-baseline2-noniso-sleep`：未隔离 + 主机睡眠导致耗时失真（T5-opus-1 记 5624s 但 `timeout 1200` 没触发）；②`aborted-baseline2-iso-lsdenied`：隔离规则用了 `file-read*`，每个 case 的首条 `ls -la` 都因 `..` 被拒而报 Operation not permitted。

**校验器** `evals/verify_genres.py`（复用 `verify.py` 的互相关函数；`tests/test_verify_genres.py` 的测试，每个任务至少一个应 pass 的理想产物和一个应 fail 的产物）：
- T5：a:0 混音里所有 speech intact 且不被削；3 个爆炸事件都在产物里；每个 boring 间隔残留 ≤1.0s（+1 帧容差）。残留用“锚点法”度量（intact 语音和在场事件做锚点，残留 = 相邻锚点在产物里的间距 − 源里除该间隔外的间距）——因为游戏音轨是周期约 1.29s 的循环音乐，逐窗口匹配会处处误报“存在”。
- T6：恰好 clip_1..3.mp4（无多余 `clip_N.mp4`）；每个 15–30s、9:16（±0.01）、有音频；逐 3s 窗口（步长 1.5s）回源里互相关定位——直播背景音是周期循环的（安静窗口会在 72s 之后同分重现），所以每个窗口取全部候选位置，唯一匹配的窗口（≥2 个）按偏移聚类，用被匹配窗口的并集覆盖 highlight 的比例（≥80%，±0.25s）判断；3 个切片对应 3 个不同 highlight；匹配不上的窗口（片头音效、响度处理）只记录。字幕有字幕流或同名 .srt/.ass/.vtt 记 verified，否则 `burned_or_missing`（只记录）。
- T7：12–18s；产物音频与 BGM 互相关 ≥0.6（全频段和 40–180Hz 低频段各算一遍取高者；BGM 是 8s 循环，偏移只能确定到循环周期，但周期是拍长的整数倍，不影响对拍判定）；scene>0.3 的画面切点 ≥4 个，落在拍点/半拍 ±34ms 内的比例 ≥80%。
- T8：横屏；时长在参考配音时长的 0.7–1.6 倍；非静音占比 ≥50%；配图 ≥2 张出现。**与任务书的差异**：配图匹配用 RGB 64×36 而不是灰度——img2（青蓝）和 img3（橙）转灰度后亮度几乎相同，分不开；字幕有字幕流/侧车文件且与脚本字符重合 ≥60% 才算 verified，其余只记录。
- T9：产物含 BGM（互相关 ≥0.4，全频段/低频段取高；任务书是 0.5，按实测标定）；truth 音节在产物里的出现 ≥6 次（互相关 ≥0.6，非极大值抑制；**只认原波形，变调后的音节找不到**）；落在拍点/半拍 ±50ms 内的比例 ≥70%（“出现点”取音节起点或能量起音点中较高者）。**补充一条**：必须有音节被重复（出现次数 > 不同音节数）——把原片放一遍也有 12 次出现，规格里只写“≥6 次”会放过。
- 所有产物另记 `loudness`（ebur128 积分响度/真峰值），供“成片质量”栏参考。

## 3.2 结果总览

pass 由 `verify_genres.py` 判定（规则见 3.1；T6/T7/T9 的规则在看到结果后根据误判做过三处修正，见 3.5）。指标详见 `evals/runs/baseline2-2026-10-28/metrics.md`（含“运行环境”列、人工核对的「自验」列、「评审者判断(非实测)」列）。

| task | model | pass | 平均调用 | 平均 $ | 说明 |
|---|---|---|---|---|---|
| T5 game_commentary | haiku | 1/2 | 12.5 | 0.02 | haiku-1：27–34s 的无聊段残留 1.3s（阈值 1.0s+1 帧） |
| T5 game_commentary | opus | 2/2 | 9.0 | 0.45 | |
| T6 stream_clips | haiku | **0/2** | 18.0 | 0.03 | 都把抽奖诱饵段/弹幕刷屏段当高光，或起点切进了惊呼；haiku-2 是重跑（原件被外部 pkill 波及，重跑结论相同） |
| T6 stream_clips | opus | 2/2 | 28.0 | 1.51 | 都避开了抽奖段；$1.5、约 8 分钟/个 |
| T7 montage | haiku | 2/2 | 8.5 | 0.02 | haiku-1 把成片写进了 `montage/` 子目录 |
| T7 montage | opus | 2/2 | 13.0 | 0.71 | |
| T8 knowledge | haiku | 2/2 | 14.0 | 0.02 | haiku-2 是重跑（原件被外部 pkill 波及） |
| T8 knowledge | opus | 2/2 | 17.5 | 0.88 | |
| T9 ytpmv | haiku | **0/2** | 18.5 | 0.04 | BGM 都放进去了，但音节没落在拍上（57% / 46%，要求 ≥70%） |
| T9 ytpmv | opus | 1/2 | 36.5 | 1.91 | opus-1 通过（能量起音对拍 98%）；opus-2 因变调被校验口径漏掉（见 3.5、卡片） |

被剔除并重跑的 6 个 case（T7-haiku-1/2、T7-opus-2、T9-haiku-1/2、T9-opus-2；素材目录里带着别的会话在 fixtures 里生成的 `.autocut-cache`，agent 读了）见第四章；上表用的是重跑结果。

## 3.3 逐 case 轨迹摘要

**T5-haiku-1**（FAIL · 7 调用 · $0.01）逐 0.5s 统计三轨响度，把音轨 1 当麦克风（“数字静音间隙、随说话起伏”），音轨 2 当游戏声，加画面变化量；规则“mic>-50dB / game>-25dB / 画面变化>8 任一成立就保留，前后各留 0.5s”；`-filter_complex_script` 不被识别，换 `-/filter_complex`；成片 25.4s、保留三轨。实测 27.43–34.43 的无聊段残留 1.3s。

**T5-haiku-2**（PASS · 18 调用 · $0.03）同样推断音轨角色，抽帧+逐帧亮度找到三次白闪；第一次分段整段全保留（50ms 窗口里游戏底噪抖动触发阈值），改 0.25s 窗口 -25dB；concat 输出标签写错，修正；24.2s。

**T5-opus-1**（PASS · 9 调用 · $0.35）“不假设常见的 OBS 分轨布局”，逐轨判角色，加 YDIF 作第二信号，抽帧 Read；本地找 whisper 无果（ggml 只有空的 for-tests 模型），改能量阈值；15ms 淡变；23.9s。

**T5-opus-2**（PASS · 9 调用 · $0.54）同路径，10ms 精度定位三下冲击，抽闪白帧核对；`ffprobe signalstats` 的错误被自己的 `2>/dev/null` 吞掉；23.9s。

**T6-haiku-1**（FAIL · 12 调用 · $0.02）借用 autobake 的 whisper 转写 150s；弹幕三个峰（含抽奖“1”刷屏），把 clip_2 定为 50–72s（抽奖段）；按字数摊时间生成 ASS 字幕；1080×1920+模糊背景；只抽看了一帧。

**T6-haiku-2**（FAIL · 24 调用 · $0.03 · 重跑；原件 16 调用/$0.02 见 4.4）这次没被打断的 ASR 搜索：`.pt` 模型 + 没有 torch/whisper 运行时 + Homebrew 里只有 575KB 的 for-tests ggml，结论同原件——“要做语音识别就得装包，违反约束”，字幕用弹幕；选 33–52/58–79/84–104，第二段是抽奖段，第三段起点晚于 highlight；字幕没渲染出来，查出 `render()` 里 `-ss` 写在 `-i` 之后、滤镜看到的时间戳是 33–53s 而 ASS 时间是 0–20s，挪到 `-i` 之前修好。

**T6-opus-1**（PASS · 29 调用 · $1.55 · 513s）对 60–70s 起疑，用频谱图判断素材音频性质，尝试 macOS Speech（缺授权）；clip_3 选 129–136s（弹幕无反应但音频形态同类）；三层字幕、模糊背景；写 unittest 并变异验证；响度 -15 LUFS。

**T6-opus-2**（PASS · 27 调用 · $1.46 · 466s）频谱图误判“音频里没有人声”，字幕改用弹幕；选 33/83/131s；弹幕排队延迟 5.7s→8 车道零延迟；单遍 loudnorm -18 → 两遍 -15.3 LUFS；镜头震动。

**T7-opus-1**（PASS · 9 调用 · $0.63）实测 BGM 为 120BPM；16 镜头编排（强拍闪白、结尾放大淡出）；ffmpeg 场景检测漏了 clip3 的切点，换逐帧 PSNR；素材原声直接丢弃但“静音”没验证。

**T7-opus-2**（PASS · 17 调用 · $0.78）手写 onset+自相关得 120BPM；独立校验（成片每帧对全部源帧做缩略图比对）；scdet 在白闪/灰画面上误检/漏检。

**T7-haiku-1**（PASS · 12 调用 · $0.02）能量相位得拍网格；镜头 2,2,1,1,1,1,2,2,2,1 秒；场景检测漏掉 5s/14s 切点，改像素 PSNR；写错帧号、`-vsync` 不被识别；成片写进 `montage/` 子目录。

**T7-haiku-2**（PASS · 5 调用 · $0.01）能量突变法找鼓点，[3,2,2,2,2,4] 秒共 5 个切点，BGM 取前 15s 末尾淡出，帧差确认 7s 处切换。

**T8-haiku-1**（PASS · 16 调用 · $0.03）`say`（Tingting）逐句合成、libass 烧字幕、标题卡+3 图；字体回落导致全角问号变半角，改 `Heiti SC`；音轨比视频短 1s，`apad` 补齐。

**T8-haiku-2**（PASS · 12 调用 · $0.02 · 重跑；原件 20 调用/$0.03/765s 见 4.4）`say`（Tingting）逐句合成、libass 烧字幕、3 图，1280×720、39.3s；这次没有 `mapfile` 问题，13 个 turn；debrief：`fc-list` 输出一大堆字体、“婷婷重复出现两次”、PIL 缺失的报错“本可以直接跳过”。

**T8-opus-1**（PASS · 18 调用 · $0.92）`say` + Noto CJK 烧字幕、Ken Burns、推移转场、进度条；自己抽帧发现转场重影、标题卡太短并修；静音检测对 13 条字幕（≤0.04s）；响度 -15.9 LUFS，Peak 没抓到。

**T8-opus-2**（PASS · 17 调用 · $0.83）同路径，字号 62→70 适配手机，关键词高亮；静音检测少了第 12 段起点（停顿 0.12s <0.3s 阈值）。

**T9-haiku-1**（FAIL · 22 调用 · $0.04）借用 autobake 的 whisper（base 繁体/turbo 简体）；发现画面里的大字逐音节出现；32 拍切片；逐个 ffprobe 发现两片 0.567/0.600s（分段 AAC 再 concat 的 priming 漂移），改 filter 图；验证的是切片边界，不是音节出现点。

**T9-haiku-2**（FAIL · 15 调用 · $0.03）whisper 词级时间戳“只取每个字的起点”；切片长度整除 0.125s 就当作踩拍；`/tmp/ytpmv_work` 中途消失（并发 case 撞名，被我的 /tmp 清理移走），改用 `/tmp/ytpmv_work_t9`。

**T9-opus-1**（PASS · 29 调用 · $1.40）无转写，按音量包络划分 12 个音节；逐字幕帧核对；65 个事件逐拍排布、变调反色镜像；把元音（而非含辅音的音节起点）对到拍点，自称 ±22ms；响度 -13.8 LUFS/-0.5dBFS。

1/22_TRAJ

## 3.4 原始观察卡片（批 2）

> 编号只是引用用，按任务顺序排，不是分类。T6 的两个 opus 与 T9-opus-1 的 PASS 是校验器修正之后的结果（见 3.5）。

- B1 [T5-haiku-1] 把音轨 1 当成麦克风，“只是根据它有数字静音间隙、响度随说话起伏来推断的，没有听过音频”(debrief)；`-filter_complex_script` 报 Unrecognized option，换成 `-/filter_complex` 才跑通；成片 25.4s，保留三条音轨；verify：27.43–34.43 的 boring 间隔残留 1.3s（>1.0s）→ FAIL。
- B2 [T5-haiku-2] 逐条音轨 0.5s RMS，看到 t1 在若干时段是数字静音（-180dB）、t0 与 t2 近似同一信号，推断 t1=麦克风、t2=游戏、t0=混音；抽帧发现画面是合成噪点，带 “autocut fixture” 字样；用逐帧亮度找到三次强闪（19.7/20.8/21.9s），对上游戏轨 19–23s；第一次分段“整段全保留”——50ms 窗口里游戏底噪在 -25~-33dB 抖动，触发了 -26dB 阈值，改 0.25s 窗口、-25dB；concat 输出标签写错，修正；最终 24.2s PASS。debrief：awk 没起过滤作用，1128 行逐帧亮度全打印。
- B3 [T5-opus-1] 明确“不假设常见的 OBS 分轨布局”，逐条轨测 0.5s 响度判角色；加画面帧间变化（YDIF）作第二信号；抽帧拼图 Read 看到“三次全屏白闪与爆响对应”；本地查 whisper 模型，“PT 缺依赖，ggml 模型是空的测试文件（for-tests）”，改用能量阈值。debrief：“精彩”靠“游戏声变响+画面剧烈变化”代表，“在真实录像里过场动画、菜单音效也会触发”；把打怪后 1.4s 空档并进保留段是“凭感觉拍板”；第一条命令 exit 1 只因顺带测 numpy。
- B4 [T5-opus-2] 想到“OBS 常见配置是混音/游戏声/麦克风”，但标题没写，靠内容判断；10ms 精度定位冲击（19.65/20.75/21.85s 三下）；debrief：`ffprobe … signalstats` 什么都没输出，错误被自己的 `2>/dev/null` 吞掉，白一步；awk 把 pts 原始值打印成 `t=207360.00s`（没换 time_base），只能手动换算；50ms 电平一长行没有时间标签。
- B5 [T5 ×4] 四个都保留了三条音轨（每条各自切分拼接）；四个都把“没说话 = 麦克风轨数字静音”当判据，把“精彩 = 游戏轨冲击 + 画面白闪”当判据；没有一个确认过混音轨与另两轨在输出里的一致性。
- B6 [T6-haiku-1] 先查本机 ASR：`~/.cache/whisper` 里有 base.pt / large-v3-turbo.pt，但 whisper-cli 要 ggml；想到“别的项目的 venv”，发现 `autobake` 的 venv 装了 whisper，直接用 large-v3-turbo.pt 转写 150s（mps 可用）；转写末尾出现“字幕志愿者 杨栋梁”，它认定是 whisper 常见幻觉并丢弃超出 150s 的段。
- B7 [T6-haiku-1] 弹幕密度三个峰：35–50s（“这也行/名场面”）、60–70s（大量“1”，它自己转写出主播说“现在开个抽奖，扣一参与”）、85–95s（“切片！”）；它认为“弹幕比语音晚 1–2s”，把 clip_2 定为 50–72s（“抽奖扣一参与→60–70s 的‘1’刷屏”）——即诱饵段；clip_1 24–50、clip_3 74–99。verify：clip_2 只碰到诱饵、未覆盖任何 highlight。
- B8 [T6-haiku-1] 用 whisper 结果按标点切短句、按字数分配时间生成 ASS 字幕，画面“模糊背景+原画居中”铺 1080×1920；debrief：字幕每句时间是按字数分配、不是逐字对齐；只抽看了 clip_3 一帧，clip_1/2 画面没看；`find` 扫整个 Codes 目录返回大量无关结果。
- B9 [T6-haiku-2（interrupted 原件）] 没找到可用 ASR（`find ~ /opt/homebrew -name …` 超时 exit 144，whisper.cpp 目录只有可执行文件），决定“字幕只能从弹幕来，在结论里说明”；抽帧看到“LIVE autocut fixture stream_vod”字样的彩条图；按弹幕密度选三段各 20s：30–50、84–104、56–76，自己写“第 3 段信号较弱（主要是‘1’刷屏），我会在结论里标注”，仍然交了；84–104 起点比 highlight 2 的开头（83s）晚，切进了惊呼中间。
- B10 [T6-haiku ×2] 都用 1080×1920 + 模糊放大背景 + 居中原画（而不是裁切），理由“直接裁切只剩约 200 像素宽”；两个都把弹幕里的“1”刷屏段当作候选高光之一。
- B11 [T6-opus-1] 从弹幕密度看到三个峰，立刻对 60–70s 起疑（“多是刷 1，可能是互动指令而非真实反应”）；把音频响度峰与弹幕峰对比，“响度峰比弹幕峰晚约 5s”，又注意到 130–135s 最响但弹幕没反应；画频谱图判断素材音频“像合成音调”，放大后确认 2–10s 是人声、30–39s 是“撞击→惊叫→笑声”；尝试 macOS 离线语音识别，`auth status: 0`（notDetermined，没报错，debrief：“要自己推断出是缺少授权”）后放弃转写，字幕改成“顶部标题/弹幕滚动/底部事件卡”三层，人声部分留空。
- B12 [T6-opus-1] clip_3 在“58–70s 弹幕密但语音少”和“129–136s 音频形态吻合但弹幕无反应”之间选了后者（理由“声音形态与前两个高光同类；‘刷 1’剪出来观众看不懂”），debrief 仍把它列为最没把握（“弹幕几乎没反应，观众可能并不觉得好笑”）。
- B13 [T6-opus-1] 全局规则渗入：`uv venv --offline` 建空 venv、写 5 个 unittest，并在临时副本里去掉“追尾”条件确认碰撞测试真能失败（变异验证）；响度约 -15 LUFS，“接近抖音常用标准”；29 个调用、$1.55、513s。
- B14 [T6-opus-1] debrief：系统提示 make_clips.py 在渲染之后被改过，“我没有读新版本，现有的 3 个 mp4 是旧版脚本渲染的”；`-ss` 后的频谱平坦度两种数值交错打印，按行配对后错位约 0.8s，结果用不上；whisper-cli 加载测试模型后什么都没输出，没有提示原因。
- B15 [T6-opus-2] 频谱图判断“音频里没有人声，只是持续的合成颤音加几段宽频爆发声”（素材里其实有 TTS 人声），因此“语音转字幕这条路走不通”，字幕改用真实弹幕（顶部标题引用弹幕、底部滚动弹幕高亮热词）；选 33s/83s/131s 三段，避开 60–70s 的“扣 1”，clip_3 的依据和 opus-1 一样：音画特征与前两段一致但弹幕稀疏。
- B16 [T6-opus-2] 自己发现弹幕排队高峰最长延迟 5.7s、片段开始前的弹幕被错误推迟到第 0s，改 8 条车道+溢出丢弃，“零延迟零丢弃”；单遍 loudnorm 只到 -18 LUFS，改两遍后 -15.3 LUFS，未到 -14 因真峰值 -1.5dBTP 限制；加了“事件瞬间镜头震动”，避开“抖音 Tab 遮挡”的顶部区域；debrief：第一次按秒算 RMS 的命令没有输出也没报错；`find` 找 ggml 时“没有结果”和“路径不对”分不清；silencedetect 一行静音都没报，“只说明不安静，不能说明有人声”。
- B17 [T6 ×4] 4 个都用“模糊放大背景 + 原画居中”做 9:16，没有一个裁切；4 个都在最终回复里承认“素材是测试图、看不出真实高光”；两个 haiku 把弹幕“1”刷屏段当候选高光，两个 opus 都识别并排除了它（opus-1 的理由是“刷 1 可能是互动指令”）。
- B18 [T6 ×4] ASR 来源：haiku-1 借用 autobake venv 的 whisper（唯一拿到转写的）；haiku-2、opus-1、opus-2 没转写（找不到可用 ggml/torch，opus-1 试了 macOS Speech 但缺授权）。
- B19 [T7-opus-1] 不信文件名里的 “120bpm”，用标准库自己做能量包络/onset 检测，“确认 BGM 确实是 120 BPM，鼓点精确对齐每 0.5s，30fps 下正好 15 帧”；做了 15s/20 镜头的编排（开场 clip1 硬切入重拍、中段提速到每拍切、强拍加亮度闪白、结尾回 clip1 放大淡出）。
- B20 [T7-opus-1] 三种自验：切点与拍网格对比（ffmpeg 场景检测 19 个切点只检出 16 个——漏掉的 4.0/9.0/12.0 都出自 clip3 的元胞自动机高频纹理，“差点让我误以为切错了”）、逐镜头源帧 PSNR 对比（20 个镜头全对上）、成片音轨鼓点在 0.5s 网格上；换成逐帧比较后又报出 57 个“多余”突变帧，全在 clip3 内部（debrief：“输出又长又吵”）。debrief：素材自带音轨“当成静音直接丢了”，但测音量那条命令（volumedetect+grep）输出为空，“‘静音’其实没验证”；“小节从哪开始是推断的（hi-hat 每 2s 变一次）”。
- B21 [T7-haiku-1] 用标准库 wave 分析鼓点相位（0ms 处能量峰值 418,864、其余相位 <5 万）→网格从 0 开始；镜头时长按拍数设计（2,2,1,1,1,1,2,2,2,1 秒）；ffmpeg 场景检测漏掉第 5s 和第 14s 的切点（分数 0.007–0.011，clip3/clip4 画面相近），它没有当成切点错，而是做像素 PSNR 对比；自己写错帧号（把 14s 当成第 210 帧，应为 420），又撞上 `-vsync` 不被这个 ffmpeg 认，改 `-fps_mode passthrough`；第 149 帧与素材 PSNR 只有 4.1dB，用平均亮度证明切点对，但“没有解释这个像素差异的来源”(debrief)。
- B22 [T7-haiku-1] 把 montage.mp4 写进了输入素材所在的 `montage/` 文件夹，而不是 cwd；prompt 只说“输出 montage.mp4”（任务描述的歧义；校验器已改为找得到同名文件就算）。
- B23 [T7-haiku-1 / T7-opus-1] 都假设“BGM 原样输出，没有测量”（haiku-1 debrief：“音频与拍点的对齐只是假设 BGM 原样输出，没有测量”）。
- B24 [T7-haiku-2] 5 个调用、$0.01：能量突变法找鼓点（“整秒位置的鼓点明显更强”），镜头 [3,2,2,2,2,4] 秒，切点全落在整秒强拍，BGM 只取前 15s 并在末尾 1s 淡出，素材原声不用；ffmpeg 场景检测抓到 3/5/9/11s 但 7s 没触发，用帧差确认；debrief：“只是简单能量突变法，没用专门的节拍跟踪，不能保证每个切点精确落在主拍上”；场景检测输出一条重复的 3.033333 没去解释。 (suggestion：卡片里 agent 自己提的改进想法，只当假设)
- B25 [T7-opus-2] 手写标准库 onset 检测+自相关估 BPM（120；“60 的峰是两拍一组的强弱拍”），验证鼓点起音与 k×0.5s 偏差 ≤1ms；做完“独立校验”——不信计划表，直接从成片检测画面切点并重新分析成片音轨鼓点；scdet 报告白闪/噪点画面是多余切点、两处灰画面之间的切换被漏掉，它判断“是校验方法本身不可靠”，改为把成片每帧与全部源帧做 64×36 缩略图比对，确认 13 个镜头归属和切点帧；clip6 彩条动画源第 8 帧和第 60 帧几乎一致，帧号无法唯一确定。
- B26 [T7-opus-2] debrief：“最没把握的是 15 秒还是 16 秒——音乐 8 秒一个乐句，16 秒收尾更完整，但你说的是 15 秒，我按字面做了，结尾停在乐句中间，靠淡出收尾”；beats.py 把 54 个峰全打印，“只需一行汇总”。 (suggestion：卡片里 agent 自己提的改进想法，只当假设)
- B27 [T7 ×4] 4 个都得到 verify 全通过：BGM 原样放入，切点全部落在拍点上；4 个都没有测量“成片里的 BGM 与原 BGM 是否一致”，只靠“切点位置”和“鼓点在 0.5s 网格上”；ffmpeg `scdet`/场景检测在 clip3（元胞自动机）、clip6（循环彩条）、灰阶画面上在 3 个 case（opus-1/opus-2/haiku-1/haiku-2）里都出现漏检或误检，agent 都改用逐帧像素比对。
- B28 [T8-haiku-1] 想到用 macOS 自带 `say`（Tingting）配音，libass 烧字幕，“不需要下载模型或装包”；每个短句单独 `say` 合成一次，字幕时间直接由各段音频时长算出；`fc-match STHeiti` 回落到 Verdana（没有中文字形），它担心字幕变方框，渲染后抽帧才发现中文能显示、但全角 `？` 掉进 fallback 字体变成半角 `?`，改指定 `Heiti SC`；ffprobe 发现音轨 37.75s 比视频 38.73s 短（amix 不补静音），加 `apad`。debrief：`say -r 190`、句末停 0.8s、逗号停 0.45s 都是“凭经验设的，没听过”；淡入淡出只抽了一帧，且那帧正好在淡入中。
- B29 [T8-haiku-2（interrupted 原件）] 脚本里用 `mapfile`，macOS 自带 bash 3.2 不支持，报错后改 `while read`；第一次重跑超过 120s 被转入后台，`sleep 25; cat …` 轮询，又写了长时间 `until` 等待；脚本先假设“7 句”，直到日志显示“共 6 句”才发现、修正映射；产出 knowledge.mp4（1280×720，36.7s）+ 同名 knowledge.srt；2 个 turn、debrief：配图与句子的对应是“按内容手写的，写死在 build.sh 里”。
- B30 [T8-opus-1] 配音用 `say`（Tingting），字幕用 libass 烧录 Noto Sans CJK SC，做了标题卡、Ken Burns 推近、转场、半透明字幕条、栏目标签和进度条，输出 1920×1080；读到全局规则后 `uv venv`（“不走网络”）；第一版转场时文字“叠加糊影”、开场标题卡太短，自己抽帧发现并改成推移转场、问句后加 0.6s 停顿；用 `silencedetect` 找语音起点与 13 条字幕时间逐条对照（≤0.04s）；-15.9 LUFS。
- B31 [T8-opus-1][T8-opus-2] 都在 debrief 里写：`say -v '?'` 列表很长、“Tingting 列了两次，看不出实际用的是哪个变体”；ebur128 那步的 grep（`I:` / `grep -A8`）没抓到 `Peak:` 行，“真峰值实际上没有拿到”（opus-1 称“算是一个疏漏”）；`grep "ass "` 查 libass 顺带匹配出一堆音频滤镜（bass、allpass、bandpass）；“make_video.py changed on disk” 的提示是自己用 Python 改文件触发的、有点误导。
- B32 [T8-opus-2] 第一条探测命令末尾 exit 1，乍看像整体失败，其实只是没装 PIL；脚本启动时核对 `script.md` 与写死的台词一致；抽帧发现“字幕在手机上偏小”，字号 62→70 重建；静音检测少了第 12 段起点——停顿只有 0.12s，低于 0.3s 检测阈值（“想了一下才明白”）；36.85s。 (suggestion：卡片里 agent 自己提的改进想法，只当假设)
- B33 [T8 ×4] 4 个都认为“配音发音（多音字，如‘散射光了’）无法确认”；没有一个对成片重新转写/朗读校验（本机无可用 ASR 或没想到）；haiku-2 留了 `.srt` 侧车，其余三个烧录字幕。
- B34 [T8 ×4] 4 个都用 macOS `say` 配音（Tingting），没有一个尝试下载 TTS 模型；3 个把配图做了 Ken Burns/转场处理，一个直接铺图。
- B35 [T9-haiku-1] 本机没有全局 ASR，但发现 `Codes/autobake` 的 venv 里有 openai-whisper、`base` 模型已缓存，直接复用；`base` 输出繁体“我真的會寫”，`large-v3-turbo` 输出简体“我真的会写”，它据此认为“两次识别一致”；debrief：“只凭低分辨率的屏幕字形判断繁体，没有确认源视频里字的真实写法”。
- B36 [T9-haiku-1] 抽帧联系表后发现“源视频里的大字按语音一个音节一个音节出现（真→會→寫…）”，所以“按 ASR 时间戳切视频就是在切真实的字”；方案：16s=32 拍，每拍一个 0.5s 切片；验证时 ffprobe 逐个看切片，发现两个切片 0.567s / 0.600s、成片 16.088s/482 帧（每段单独 AAC 编码再 stream-copy concat 引入 priming 漂移），改成滤镜图（输入标签只能消费一次，需要 split/asplit 32 路），最终 16.000s/480 帧；debrief：逐段渲染日志太多，“掩盖了两段时长异常，直到用 ffprobe 逐个检查才发现”。
- B37 [T9-haiku-1] verify：BGM 在（低频互相关 0.98），音节出现 14 次/10 种，但只有 57%（能量起音）落在拍点/半拍 ±50ms → FAIL；它自己的核对是“32 个拍的切片时间与音乐瞬态对齐”（切片边界），不是“音节出现点”。
- B38 [T9-haiku-2] 没转写出可用台词（`whisper-cli` 用自带 `for-tests` tiny 模型“跑出来是空结果，白跑一次”，debrief），改用 whisper 词级时间戳“只取每个字的起点”，“这”标 1.2s；切片长度都是 0.125s 整数倍；debrief：“拍点同步只是靠‘切片长度都是 0.125s 整数倍、起点在网格上’推出来的，没有用音频分析测成片里的实际起拍”；峰值 0→-0.6dB。 (suggestion：卡片里 agent 自己提的改进想法，只当假设)
- B39 [T9-haiku-2] `/tmp/ytpmv_work` 在两次命令之间消失，`cd` 报错，它花一轮查 `/tmp` 状态后改用 `/tmp/ytpmv_work_t9`。（基础设施效应：同一时段的 T9-haiku-1 也用了 `/tmp/ytpmv_work`，它结束后我的 /tmp 残留清理把该目录移走了——并发 case 撞名，见第四章。）
- B40 [T9 haiku ×2] 都没把“字出现在拍点”作为验证对象：haiku-1 验证的是 32 个切片边界，haiku-2 验证的是切片长度整除 0.125s；verify 里音节出现点对拍 57% / 46%，BGM 都放进去了（互相关 0.98/0.94）。
- B41 [T9-opus-1] 本机没有可用 whisper 模型，用 silencedetect 把人声按停顿切成 7 段（分两句，中间约 1s 停顿）；“不能只信文件名里标的 120bpm”，实测鼓点从 0s 起每 0.5s 一拍、2s 一小节；抽帧发现画面是测试图卡，“上面叠着随台词逐字出现的黄色大字”，拿来核对切点；第一次裁字幕区域选错了位置（字在画面中间偏左）。
- B42 [T9-opus-1] 没有转写，靠音量包络划分 12 个音节（5+7），推断是“我真的会谢 / 这也太离谱了吧”；debrief：“有几个字（比如‘谢’显示成‘射’）其实是猜的，我也从来没听过音频本身”；两次字幕拼图读不出来（裁错位置、字高不固定行叠在一起），最后靠音量包络定切点。
- B43 [T9-opus-1] 编排：按小节逐步加密 16 格，重复字镜像放大、升调反色，共 65 个事件；对齐检查发现“谢/这/太”因弱辅音被误判为起点、比拍点晚 40–100ms，于是“按唱歌处理”把辅音提前（最多 70ms）、让元音落在拍点；自己的检查报出 -60ms，查出是“检测窗口撞上了前一个字的尾音”的假问题；最终声称 65 个事件都在拍点 ±22ms 内（中位 -2ms）。
- B44 [T9-opus-1] 发现“降八度的字不能再用镜像效果——反色和镜像叠加会让彩条互相抵消，结尾‘吧’字显得平淡”，改为反色+1.6 倍居中特写；响度 -13.8 LUFS、峰值 -0.5dBFS，“无削波”。debrief：每 20ms 一行的包络 ASCII 图有 250 行；`time` 套 `tee` 和 `grep` 每段管道都打计时，全是噪音。
- B45 [T9-opus-1] 对拍判定的口径差异：它对的是“元音/能量起音”，verify 按“音节起点（含辅音）”算只有 51%，按“能量起音”算 98%——初版校验器因此误判为失败，已改成两种口径取高者。
- B46 [T5-haiku-1][T7-haiku-1] 都撞上 ffmpeg 9 的变更：`-filter_complex_script` 不再被识别（换 `-/filter_complex`），`-vsync` 不再被识别（换 `-fps_mode passthrough`）；报错信息里没有替代写法。
- B47 [T5-opus-1][T6-opus-1][T6-opus-2][T9-haiku-2] 都碰到 whisper-cli 自带的 `for-tests` tiny 模型：加载后“什么都没输出/空结果”，没有提示原因，debrief 里都写成“白跑一次”。
- B48 [T6-opus-1][T8-opus-1] 读到全局规则后“项目规则要求在 venv 里跑 Python”，为只用标准库的脚本 `uv venv --offline`；T6-opus-1 还写了 unittest 并做变异验证。
- B49 [T5-haiku-2][T6-haiku-2（原件）][T6-opus-2][T9-opus-1] 从抽帧里读到烧录文字 “autocut fixture”/“LIVE autocut fixture stream_vod”，都把它当成“这是合成测试素材”的证据，据此调整了对素材的判断（“看不出真实高光内容”）。
- B50 [T5 ×4][T6 ×4][T7 ×4][T8 ×4][T9 ×3] 19 个 case 的 debrief 都承认“没有听过音频”；成片里的 BGM/配音是否正确，没有 agent 用互相关等方式量过。T7-haiku-1 debrief：“音频与拍点的对齐只是假设 BGM 原样输出，没有测量”；T9-haiku-2：“拍点同步只是靠‘切片长度都是 0.125s 整数倍、起点在网格上’推出来的”。
- B51 [T7-haiku-1] prompt 说“输出 montage.mp4”，它把文件写进了 `montage/` 子目录（输入素材所在处）而不是 cwd；agent 没有问，也没有在最终回复里强调位置差异。
- B52 [T8-haiku-2（interrupted 原件）][T9-haiku-1] 都用到了超过 120s 的命令被转入后台后再用 `sleep`/`until` 轮询等待；T8-haiku-2 该 case 墙钟 765s。
- B53 [T9-opus-2] 想到“python3.14 自带 numpy/scipy 可以用来分析”，趁全盘 `find /` 搜 ggml 模型（超时转后台，最后什么也没找到，debrief“纯属浪费”；`mdfind` 还多打了一行无关的 locale 警告），先用能量包络法把台词切分；抽帧发现画面逐个显示大号汉字，读出“我真的会谢 / 这也太离谱了吧”，并写“都是常见鬼畜梗”；裁剪位置偏了一次（字被切掉一半）。
- B54 [T9-opus-2] `/tmp/ytpmv_work` 在两次命令之间不见了，它写“这个环境的 /tmp 似乎会被清理”，改把中间文件放进工作目录 `work/`、全用绝对路径。（基础设施效应：并发的 T9-haiku-1 同样用了该目录，它结束后我的 /tmp 残留清理把它移走。）
- B55 [T9-opus-2] 音乐分析：“8 个完全相同的小节循环，1/3 拍底鼓、2/4 拍军鼓”，低频里反复出现 110Hz，细看是贯穿全曲的 A1（55Hz）808 贝斯；据此把人声音节映射到 A 小调五声音阶，做“花栗鼠”高潮；用 rubberband 逐字×变调×拉伸组合渲染，在渲染结果上重新测起音点。
- B56 [T9-opus-2] 自己的验证发现“我”系统性偏高约 4 个半音（用整字 F0 中位数而不是起音段算变调量）和一个验证脚本的切片 bug；改成闭环调音（先渲染、测起音后 10–60ms 的实际音高、按误差修正重渲）：音准中位误差 131→9 音分，±1 半音内 32%→99%；“谢→E3”降调过大导致 rubberband 不稳、音色发闷，把那组四连打改成 G3-A3-C4-D4。
- B57 [T9-opus-2] 响度 -19.1 LUFS→加 +5dB 增益和限幅器到 -14.8 LUFS，但真峰值 +0.2dBFS，把限幅阈值从 -1 降到 -2dBFS，并在验证脚本里加“真峰值 ≤-0.5dBTP”检查，得 -0.7dBTP。
- B58 [T9-opus-2] 权衡全局规则和约束：“你要求不装新包以节省流量，我跳过建 venv（会触发重装 numpy/scipy/PIL），直接用 Homebrew 自带的 python3.14 运行”。
- B59 [T9-opus-2] debrief：音准检测用的是和生成时同一套测量函数，“只能证明内部一致，不能证明人耳听着在调上”；卡拍是在限幅前的纯人声轨上测的，“没有在最终限幅后的混音里逐字复测”；画面只抽查了第 1、2 小节和第 3 小节开头；“台词是我从画面字幕上读的，没有拿语音识别核对过”；构建日志每次打 79 行逐字明细。从成片 mp4 解码音频与混音做互相关，偏移为 0。
- B60 [T9-opus-2] 44 个调用、45 个 turn、$2.41、883s，是批 1+批 2 全部 36 个 case 里最贵最慢的；verify：BGM 在（低频 0.97），能量起音对拍 100%，但“台词音节在产物里只出现 3 次 <6”——它把每个音节都变了调，波形互相关找不到被变调的音节。
- B61 [T9-opus-1][T9-opus-2] 两个 opus 都用了变调（升调反色 / A 小调五声音阶），这是 ytpmv 的核心手法；而任务书的“音节出现 ≥6 次”按原波形计数，T9-opus-1 靠一部分未变调的事件（41 次）过线，T9-opus-2 没有。
- B62 [T6-haiku-2（重跑）] 没被打断的 ASR 搜索得出相同结论：“本机没有装 openai-whisper 或 faster-whisper，Homebrew 那个 ggml 模型只是测试用的 tiny 版（575KB，whisper.cpp 的测试夹具），机器上没有 torch，要做语音识别就得装包，这违反你的约束”；它没有去找别的项目的 venv（T6-haiku-1 借用了 autobake 的）。所以原件里被外部 pkill 打断的 `find` 没有改变结果。
- B63 [T6-haiku-2（重跑）] 再次按弹幕密度选段：35–47s（“名场面/这也行？”）、60–70s（“大量‘1’，中间夹着‘这把能赢吗’，像是倒计时或紧张时刻，但从弹幕无法确定画面发生了什么，我会在最后说明”）、84–96s；仍把抽奖“1”刷屏段当作第二个高光交付；verify：clip_2 58–79s 只碰到诱饵，clip_3 84–103.5s 的起点晚于 highlight 2（83s），没有完整覆盖。
- B64 [T6-haiku-2（重跑）] 渲染后抽帧发现字幕和标题都没出现、整帧发灰：单独渲染 ASS 正常，用同一滤镜链对比三种顺序、用像素统计判断，统计命令在没匹配到字段时非零退出把脚本带崩（debrief：“浪费了一轮”）；最后查出源视频在 33s 处本来就是淡灰色，真正的 bug 是 `-ss` 写在 `-i` 之后——滤镜看到的时间戳是 33–53s，而 ASS 时间相对 0–20s，字幕整段错过；挪到 `-i` 之前后修好。debrief：ffmpeg 的 “image2 pattern” 警告让它误以为输出没写出来。
- B65 [T8-haiku-2（重跑）] 计划用英文写（“Voice: macOS `say` with Tingting. It's offline, already installed, and needs no model download”），逐句合成以得到精确时长；12 个调用、13 个 turn；抽 3 帧看字幕，用 volumedetect 确认“声音有电平（均值 -18.8dB、峰值 -2.6dB）”；debrief 与其余 T8 case 相同：配音读音（“瑞利”“散射光了”）和停顿听感没法判断。

## 3.5 校验器在看到结果后修正的三处（如实记录）

批 2 的结果先用初版校验器跑出，读 transcript 时发现三类误判，修正后对所有保留的沙盒重新校验（`evals/verify_case.sh`），测试同步更新（`tests/test_verify_genres.py`，35 个）：
1. **T6 片段回源定位**：初版要求开头/中段/结尾三个窗口都回得到源里同一个偏移；两个 opus 的片段加了片头音效、响度处理，开头和后半段窗口对不上，被误判“不连续”。改为逐 3s 窗口回源、唯一匹配按偏移聚类（≥2 个唯一窗口），用被匹配窗口的并集判断覆盖 highlight 的比例（≥80%）；匹配不上的窗口只记录（`foreign_audio_windows`）。两个 haiku 的失败不受影响（诱饵段/切进惊呼是真失败）。
2. **T7/T9 的 BGM 互相关**：全频段波形互相关对变调、压低、响度处理、滤波很敏感（T9-opus-1 只有 0.084，但 BGM 明明在）。改为全频段和 40–180Hz 低频段各算一遍取高者（低频段：未处理 0.97–0.99，压低/变调后 0.47，没有 BGM 0.12）；T9 阈值按实测标定为 0.4（任务书 0.5）。同时修掉一个连带问题：对拍判定原先用了一个得分 0.084 的无效窗口给出的 BGM 偏移（-1.005s）。
3. **T9 音节“出现点”的口径**：任务书没定义。初版按 truth 里音节区间的起点（含辅音）算对拍，T9-opus-1 只有 51%；它明说把元音/能量起音对到拍点，按“能量起音”（包络达峰值 25% 的时刻）算是 98%（中位误差 1ms）。改为两种口径都算、取较高者（haiku 两个仍只有 57%/46%）。
另外：T7 的成片位置放宽（根目录没有就在前两层找同名文件，agent 常把成片放进输入素材文件夹）；T5 的残留阈值加了一帧容差（1.005s 这种几毫秒的超出是测量噪声）。

---

# 第四章 隔离泄露与未隔离运行（补充证据）

这一章记录两类东西：**未隔离的运行**（最早的批 1，沙盒只是 cwd 在仓库外）和**隔离过程中暴露的各种泄露/污染/基础设施事件**。它们本身就是 ACI 证据：agent 会主动扫盘找工具和模型、读到旁边残留的分析缓存、用宽泛的 `pkill` 清理进程。第二、三章的结论以隔离版为准。

## 4.1 未隔离版批 1（`baseline-2026-10-28`，环境 noniso）：结果总览

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

## 4.2 逐 case 轨迹摘要（未隔离版批 1）

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

## 4.3 原始观察卡片（未隔离版批 1，U 系列）

> 编号只是引用用，按 case 顺序排，不是分类。这一批的 agent 能读到整个文件系统。

**T1-haiku-1**
- U1 [T1-haiku-1] `ls` 已经看到工作目录里有 talk_zh.mp4，第一条命令仍然 `ls -la /Users/lishuyu/Codes/autocut` 并 `find / -name "talk_zh*"`：从 cwd 路径里的 “autocut” 推断项目目录存在，想确认工作目录里这份是不是“那份”。
- U2 [T1-haiku-1] `find /` 的输出（1.3k 字符）里同时有仓库 fixtures、scratchpad/calib 里的同名副本和 talk_zh.truth.json 的文件名；它认为“工作目录里的就是要处理的，其他副本不动”。debrief：这一步“可以省掉”。
- U3 [T1-haiku-1] 直接选 `silencedetect=noise=-30dB:d=0.3`，没有先测音量；debrief：“-30dB 是凭经验定的，没有用音频实测过”。这段录音底噪约 -52dBFS，所以恰好有效。
- U4 [T1-haiku-1] 把“对成片重跑同一个 silencedetect，最长剩余 0.83s”当作确认；debrief 承认没确认切点是否落在字间、轻声段是否被误切、音画是否严格同步。

**T1-haiku-2**
- U5 [T1-haiku-2] 同样选 -35dB 初筛，没测噪声；把 0.6–0.8s 的 4 处停顿“判为自然停顿保留”，自定“≥1s 才算长停顿”。
- U6 [T1-haiku-2] 整个任务 3 次调用，最终回复明说“只用音量判断，没有听过音频”，把“有意留的节奏停顿也会被剪”列为拿不准。

**T1-opus-1**
- U7 [T1-opus-1] 先测 100ms 一格的 RMS 包络（返回 7,232 字符，显示被截断），从“说话 -14~-23、停顿 -47~-56、中间 25dB 空档”选 -40dB，明确“不随意设 -30dB”。debrief：“每 100ms 一个的音量列表太长了，只需要静音段起止加一个分布”。 (suggestion：卡片里 agent 自己提的改进想法，只当假设)
- U8 [T1-opus-1] silencedetect 把 31.46–34.87 的 3.4s 停顿劈成两段（中间一个 1ms 的小杂音），它认为那是同一段停顿，在脚本里加“间隔 <0.1s 合并”。debrief：“一开始容易被误读成两个停顿”。
- U9 [T1-opus-1] 看到 24.4s、32.5–33.2s “比底噪高 4–6dB 的很轻的声音”，当作呼吸/杂音一起剪掉或缩短；最终回复把这条列为拿不准，debrief：“只看了音量数值，没听过，不能排除是很轻的说话”。
- U10 [T1-opus-1] 脚本写完后“按本地 Python 规范”用一段 python 批量把 print 换成 logging、补类型标注，再 `uv venv -q .venv` 并运行；debrief：“这一步很笨，一开始就按规范写更干净”。（全局 CLAUDE.md/rules 的 Python 规范渗进了剪辑任务） (suggestion：卡片里 agent 自己提的改进想法，只当假设)
- U11 [T1-opus-1] 抽三个剪点前后的帧拼成 PNG 并 Read，读到画面里的烧录时间码和文字 “autocut fixture talk_zh”，用时间码核对三个剪点落在计划帧上；最终回复写“画面是彩条测试图，判断不了跳切明显不明显”。
- U12 [T1-opus-1] debrief 说“脚本在我跑完之后被改过，现在磁盘上的版本我没有重新跑”；transcript 里脚本的 python 批量改动发生在 dry-run 和渲染之前，之后只有 `rm -rf .venv`。（debrief 自述与记录不符）

**T1-opus-2**
- U13 [T1-opus-2] 包络命令返回 13,989 字符；debrief：“400 多行太长，只输出低于 -40dB 的区间就够了”。 (suggestion：卡片里 agent 自己提的改进想法，只当假设)
- U14 [T1-opus-2] `for r in "9.3 11.1" …; do set -- $r; ffmpeg -ss $1 -to $2 …` 在 zsh 下输出全空，原样再跑一次仍为空，第三次才改写；debrief：“zsh 的 `set -- $r` 不会按空格拆分，ffmpeg 收到的是空参数”。
- U15 [T1-opus-2] 用“非静音总长 原片 26.71s / 成品 26.83s，没有吃掉任何字”作为不吃字的证据。
- U16 [T1-opus-2] debrief 补了最终回复里没有的事实：原片 24.45s 的 0.506s 停顿没剪，成品里测出来却只有 0.384s；它推测是重编码让阈值附近边界变了，“没有核实”。

**T2-haiku-1**
- U17 [T2-haiku-1] 第一次 silencedetect 在 -30/-35/-40dB 三个阈值下都没输出（末尾 grep 无匹配，整条命令退出码 1），它认为“底噪可能高于 -30dB”，把阈值扫到 -25/-20/-15，并用逐 0.5s RMS 交叉验证后选 -25dB。（debrief：“一开始误以为阈值检测失败，后来才发现是底噪高于 -30dB”）
- U18 [T2-haiku-1] 规划脚本的输出里有一个 0.3s 的“保留段”，它读数字发现是两段相邻静音之间 0.00002s 的浮点缝被 0.15s padding 放大；debrief：“靠读数字发现的，不是靠工具报错”。
- U19 [T2-haiku-1] 对第一版成片复测，发现残留一段 0.54s 停顿（6.24s），归因于自己加的 MERGE_GAP 合并逻辑，删掉后复测为空；最终 23.60s。（复测抓到了自己的 bug）
- U20 [T2-haiku-1] 最终回复写“共剪掉约 11.8 秒”；实测 11.74s。

**T2-haiku-2**
- U21 [T2-haiku-2] silencedetect 在 -32/-36dB 无输出，它一度以为 ffmpeg 报错，查看原始输出后认为“silencedetect 按峰值判断，教室瞬时噪声让安静段‘不算静音’”，改用 Python 标准库算 100ms RMS；debrief：“-28dB 的输出与 RMS 数据对不上，我没有深究就转向了 RMS”。
- U22 [T2-haiku-2] `for s in $SEG` 在 zsh 下只循环一次，ffmpeg 报错里的 `25nd=34.55` 很难一眼看出原因；它改写成 bash 脚本。
- U23 [T2-haiku-2] 成片上 RMS 复测还有 56 个窗口 <-30dB，查最长连续静默 1.0s，认为是句间停顿并接受；这个 case 只剪了 9.54s（opus 剪 11.7–12.3s）。
- U24 [T2-haiku-2] 最终回复“一共剪掉 9.54 秒”与实测一致；列出了被剪的 5 段原片时间。

**T2-opus-1**
- U25 [T2-opus-1] 带通 300–3400Hz 后测噪声（一次命令返回 18,613 字符），看到原始音轨 0.7s/13.5s 附近的 -32dB 杂音在带通后降回底噪，认为是低频桌椅声而不是说话。
- U26 [T2-opus-1] 脚本里的 silencedetect 一处静音都没找到，脚本只返回 “Exit code 1”；它用 `bash -x` 重跑才看明白，转为自己做 50ms RMS 分段。debrief：“那一轮基本白跑”。
- U27 [T2-opus-1] 第一段起点出现 -0.150；它读数字发现并查出是 awk 里未初始化的 `n` 当数组下标变成空串；debrief：“这是我自己写出的 bug”。
- U28 [T2-opus-1] 确认手段：人声频段内 >-40dB 的片段数剪前剪后都是 390 个，最长安静 0.45s，音画差 17ms；debrief：没听、没看画面。

**T2-opus-2**
- U29 [T2-opus-2] 第一条命令 `cd` 失败：路径写成 `…/-Users-lishuyu-Codes-autocut-457cba72-…`（连字符形式，像 Claude 项目目录名）而不是真实 pwd；`ls` 对比后改用相对路径。（T4-opus-1 也有同样的 `autocut-457…` 笔误）
- U30 [T2-opus-2] 单看音量分不清 9.2s（0.35s）和 33.7s（全片最响）那两段短声是字还是咳嗽，用 `showspectrumpic` 生成频谱图并 Read PNG，凭“谐波条纹”判断是人声而保留；debrief：“学生小声说话、笑声、哼声也有谐波，会被当成讲课”。
- U31 [T2-opus-2] 为找本地 whisper 模型启动 `find ~ /opt/homebrew -name 'ggml-*.bin'`，跑了两分多钟没结果；它用 ToolSearch 加载 TaskStop 停掉，改用频谱图。
- U32 [T2-opus-2] 最终回复“一共剪掉 12.29 秒”是按计划算的值；成片实测剪掉 12.27s，debrief 自己指出了这个差。
- U33 [T2-opus-2] 读到全局规则后“按 Python 规范”给脚本补 logging/类型注解并 `uv venv`，运行完 `rm -rf .venv`。

**T3-haiku-1**
- U34 [T3-haiku-1] 第一条命令就 `find / -name 'talk_zh*'` 并 `command -v whisper whisper-cli …; ls ~/.cache/whisper ~/.cache/huggingface`：它认为要先“听懂内容”才能剪口误和“嗯”，在找本机的转写工具。
- U35 [T3-haiku-1] 找到 whisper-cli 和 `/Users/lishuyu/Codes/autocut/models/ggml-large-v3-turbo.bin`，认为“本机已有、没有下载，可以直接用”；最终回复明写“模型直接用了 autocut 项目 models/ 下已有的文件”。
- U36 [T3-haiku-1] 相信 whisper 的 token 时间戳：把“嗯”定在 12.84–13.23s（真值 14.51–14.74s，偏早约 1.7s），据此切掉 12.77–13.30——那是“素材”的尾部——以及 20.10–27.60。
- U37 [T3-haiku-1] silencedetect 显示 12.8s 附近“嗯”前后没有停顿，它没有怀疑 whisper 时间戳整体偏移，转去用 10ms RMS 包络找“嗯”的边界，认定 12.84–13.23 的元音段就是“嗯”。
- U38 [T3-haiku-1] 对成片再转写看到“素材,按,然后”，担心“按”是“嗯”的残留，查能量后判“按”是“材”的尾音；最终回复一边写“‘嗯’已剪掉”，一边把“按”列为“需要你确认的一处”。（实测 filler 仍然完整在 14.5s）
- U39 [T3-haiku-1] debrief：“whisper 逐词时间戳很多 token 起止相同或明显错位，只能做大致定位”；“第二次转写文本也有误识别（‘准备使素材’），文本比对只能当粗略检查”。

**T3-haiku-2**
- U40 [T3-haiku-2] `ls -la /Users/lishuyu/Codes/autocut/models/` 后选 `ggml-large-v3-turbo-q5_0.bin`，转写产物放 /tmp/talkclean。
- U41 [T3-haiku-2] 第一版成片转写仍出现“嗯”，它判断“字级时间戳可能偏早”，把切点从 12.80 提前到 12.43，并用 `-dtw` 对齐——DTW 给出的时间戳与普通模式相同（仍标 12.80–12.98）；它又推断“12.43–12.80 的逗号其实是嗯的发声开头”。
- U42 [T3-haiku-2] 能量图与 whisper 字级时间戳“对不上”，它的结论是“whisper 的时间戳本身就粗”；debrief 称 RMS 输出“反而误导了判断”。没有去找能量图上 14.5s 的孤立 0.23s 短音。
- U43 [T3-haiku-2] 最终转写里“嗯”消失、拼接处冒出“按”；它判断“按”在输出 11.89s、在保留段内，“不是我的剪切造成的”，据此称成功。debrief：“可能是噪声也可能是‘嗯’的残留”。（实测 speech#2 partial 0.66，filler intact 0.996）
- U44 [T3-haiku-2] 把口误前 2.4s 的空白和口误一起剪掉（19.94–27.76），只保留 0.4s 自然停顿。

**T3-opus-1**
- U45 [T3-opus-1] 先 `command -v whisper whisper-cli mlx_whisper faster-whisper`，再 `find ~ /opt/homebrew -maxdepth 6 -name "ggml-*.bin" …`，选 `~/Codes/autocut/models/ggml-large-v3-turbo.bin`，理由“只读引用，不复制不改动；中文比 base 好”。
- U46 [T3-opus-1] `-ml 1 -sow` 只给出句子级结果，`-dtw` 静默失效（`t_dtw` 全是 -1，没有任何报错）；它发现 token 时间戳与静音区间“整体偏了 1–2 秒”，不再用 whisper 时间戳定切点。
- U47 [T3-opus-1] 把静音切出的“语音岛”逐个单独转写，核实 14.51–14.73（0.22s）是“嗯”；担心 prompt 里写了“嗯”会诱导 whisper，去掉 prompt 重转写并检查音量交叉验证。
- U48 [T3-opus-1] 对成片整片重转写，“嗯”、错的那句、“不对，我重新说一遍”都没了、其余一句不少；音频 32.707s、视频 32.700s。debrief：“没真的听过；‘嗯’的识别是靠 whisper 和音量两种方法交叉验证的，同样没人工听过”。

**T3-opus-2**
- U49 [T3-opus-2] `mdfind -name "ggml"` 找到仓库 models 下的模型，只读引用。
- U50 [T3-opus-2] whisper 把“嗯”标在 13.74s，“第二步是把没用的”七个字全标在 21.94s；它用 20ms 能量包络找到 14.50–14.74 的孤立短音，单独转写这一小段确认是“嗯”，剪点定在 14.30–15.20 和 22.10–27.80（接缝处约 -55dB）。debrief：“单独转写这么短的片段本身也不太可靠”。
- U51 [T3-opus-2] 抽四个剪点的帧 Read 后认定“画面是测试图，剪点跳帧不影响观感”。
- U52 [T3-opus-2] 成片视频流比音频短 1 帧，它先确认是不是原片自带的差异（是）；对成片再转写用的是同一个模型，debrief：“如果它两次都漏掉别的口头禅，我也发现不了”。

**T4-haiku-1**
- U53 [T4-haiku-1] 选 FCPXML 1.10，理由“FCP 和 Resolve 都能导入、能保留素材路径和 marker，优于 EDL”；只剪 ≥0.6s 的停顿，两端各留 5 帧，每个切点加 marker。
- U54 [T4-haiku-1] 自己写的一致性检查抓到 bug：文件末尾 silencedetect 给出了 `silence_end`，尾部静音分支没走，留了 0.17s 的碎尾段；它修了条件，时间线变成 7 段 31.57s。
- U55 [T4-haiku-1] 复核命令里 `-map [out]` 引用了已被 silencedetect 消费的标签，ffmpeg 退出码 234；它先判断“是我分析命令写错，不是素材问题”，去掉 `-map` 重跑。
- U56 [T4-haiku-1] 验证手段是 xmllint + 把保留段在内存里拼起来再跑 silencedetect（最长剩余 0.42s）；debrief：“从未在任一软件里打开过，最担心 Resolve 拒绝导入”。

**T4-haiku-2**
- U57 [T4-haiku-2] 选 xmeml（FCP7 XML），理由“比 EDL 能保留音视频的链接关系”；产物 5 个视频片段 + 5 个音频片段；写完先改掉自己觉得“绕的 link 逻辑和空循环”。
- U58 [T4-haiku-2] 验证：解析自己的 XML 核对每段 in/out 与时间线位置连续，再内存拼接复测（最长剩余 0.84s）。debrief：“XML 没在 FCP/Resolve 里导入过，音频声道映射和素材重新链接也没验证”；“`|| true` 会吞掉真正的错误，我应该更谨慎”。 (suggestion：卡片里 agent 自己提的改进想法，只当假设)

**T4-opus-1**
- U59 [T4-opus-1] 先 `command -v ffmpeg ffprobe auto-editor python3 uv` 并检查 numpy；之后 `ls -d /Applications/Final\ Cut\ Pro*.app /Applications/DaVinci\ Resolve*`，发现都没装，因此“没有官方 DTD 可做严格校验”。
- U60 [T4-opus-1] 导出 FCPXML 1.8 主文件 + CMX3600 EDL 备份 + CSV 剪点表，每个片段引用完整源文件，“被剪掉的停顿还在素材里，可以在软件里拖回”。
- U61 [T4-opus-1] 按全局规则 `uv venv` 并 `uv pip install pytest --offline`（离线缓存），写了 8 个 pytest；测试失败时 subprocess 堆栈很长，有用的只有最后一行 `Unable to parse "end" option value "143/15"`；它据此把时间写法从 `143/15s` 改成 `286/30s`，理由“FCP 惯用的帧时长分母，避免某些导入器挑剔”（没有验证）。
- U62 [T4-opus-1] debrief：`<format>` 没写 name、asset 用 `src` 而不是 `media-rep`、`<!DOCTYPE fcpxml>` 的写法“都是我按印象定的，没有官方 DTD 也没有真软件验证过”。

**T4-opus-2**
- U63 [T4-opus-2] 从 cwd 路径里看到 “autocut”，`command -v ffmpeg ffprobe autocut auto-editor` 并 `ls ~/Codes/autocut | head`；没发现可用的 `autocut` 命令，继续用 ffmpeg。
- U64 [T4-opus-2] -45dB 的 silencedetect 一个结果都没有，与自己的 RMS 表（停顿段约 -50dB）对不上，它用“silencedetect 按采样峰值判断”解释，没有验证，改选 -35dB。debrief 承认“没去验证这个假设”。
- U65 [T4-opus-2] 导出 FCPXML 1.9 + EDL + CSV，另写了 8 个 unittest；把按剪辑表重组的音频 `_check_audio.wav` 留在工作目录供试听；抽帧对照图里 3 处晚了 1 帧，归因于“抽帧时四舍五入”，没有用精确帧号重抽就写进了最终回复。
- U66 [T4-opus-2] 最终回复给了 FCP“文件→导入→XML”、Resolve“文件→导入→时间线”的操作步骤，同时 debrief 说 format 没写 name、用了 1.9 版的 media-rep、Resolve 对 1.9 和片段 marker 的支持都是凭规范记忆。

**跨 case**
- U67 [全部 16 个] debrief 都承认“没有听过音频”；没有任何一个 agent 拿成片音频和源音频做过逐段比对（互相关/波形）；复核手段只有：重跑同一个检测器（silencedetect / RMS）、重转写、时长与帧数、抽帧。
- U68 [全部 16 个] 把“音视频流时长相等”当作音画同步的检查（T1-opus-1 “33.467s 一帧不差”，T2-haiku-1 “23.600 / 23.598”，T3-opus-1 视频 32.700 / 音频 32.707，T3-opus-2 “视频流比音频短 1 帧”）；没有 agent 检查口型/事件级同步。
- U69 [T2-haiku-1][T2-haiku-2][T2-opus-2] init 事件里这三个 case 的可用工具数是 26，其余 13 个是 107：同一套配置下，agent 看到的工具集不稳定。
- U70 [T1-opus-1][T1-opus-2][T2-opus-2][T4-opus-1][T4-opus-2] 用户全局的 Python 规范（logging、类型注解、`uv venv`、pytest）进入了剪辑任务：这些 case 比同任务的 haiku 多出补注解、建 venv、写测试的调用（T4-opus-1 19 次调用、$1.18）。
- U71 [T1-opus-1][T4-opus-2] 运行 uv 时看到 `[uv-shim] network=expensive -> offline`，把它当成“确认了没走网络”的证据；debrief 称“无关噪音”。
- U72 [T2 ×4] 要求汇报“一共剪掉多少时间”：4 个 case 报的数字都与实测一致（±0.02s）；其中 opus-2 报的是计划值，自己在 debrief 里指出了和成片差 0.02s。
- U73 [T3-haiku-1][T3-haiku-2] 两个 haiku 都在同一件事上失败：相信 whisper 时间戳 → 切错位置 → 用同一个 whisper 复核看到“按” → 解释为无害 → 声称成功；两个 opus 都在 whisper 时间戳与静音区间对不上时放弃了时间戳、改用能量/静音找“嗯”。
- U74 [T3 ×4] 四个 case 都能找到 `whisper-cli` 和一个 ggml 模型（均在仓库 `models/` 下），都把“本机已有”当作可用，没有一个考虑过这个模型文件“不是为这个任务准备的”。

## 4.4 泄露、污染与基础设施事件（卡片，L 系列）

> 同样从 agent 的视角写它相信/看到了什么；基础设施的原因写在括号里，供读者判断这条观察在多大程度上是我们自己的环境造成的。已在 4.3 里的现象在这里只给指路。

**agent 主动扫盘、找工具、读到旁边的东西（指路到 U 卡片）**
- L1 `find / -name "talk_zh*"` 扫盘：U1、U2（T1-haiku-1）、U34（T3-haiku-1），T3-haiku-2 的 `find` 列出 `/Users/lishuyu/Codes/autocut/...`（U40），T4-haiku-1/2、T4-opus-1/2 也都跑了（U63 等）。输出里同时有仓库 fixtures、scratchpad/calib 里的同名副本和 `talk_zh.truth.json` 的文件名；没有 agent 读过 truth 的内容。
- L2 用了仓库里的模型：四个 T3 case（haiku ×2、opus ×2）都把 `/Users/lishuyu/Codes/autocut/models/ggml-large-v3-turbo(.q5_0).bin` 当“本机已有、不算下载”（U35、U40、U45、U49、U74）；隔离后读不到，agent 各自去用户主目录里找别的 ASR（第二章 A1–A16）。
- L3 读到烧录文字：fixture 画面左下角 “autocut fixture talk_zh”，T1-opus-1（U11）、T4-opus-2（U65）在抽帧里读到，并把它当“这是测试素材”的证据；批 2 里 T5-haiku-2、T6-haiku-2（“LIVE autocut fixture stream_vod”）、T6-opus-2、T9-opus-1 同样读到（B 系列跨 case 卡片）。
- L4 看到 cwd 里的 “autocut”：U1（T1-haiku-1 `ls /Users/lishuyu/Codes/autocut`）、U63（T4-opus-2 `command -v autocut`）、A2/A18（隔离版里 T3-haiku-1、T4-haiku-1 想看项目 venv/现成导出代码，被拒后“不去绕过它”）、A23（T4-opus-1 `ls ~/.claude/projects/-private-tmp-claude-501--Users-lishuyu-Codes-auto…`）。

**素材目录里的隐藏分析缓存（批 2 的 6 个污染原件，`contaminated/`）**
- L5 [T7-haiku-2] 看到 `montage/.autocut-cache` 里有 beats.json 和 cuts.json，“它们是别人生成的缓存，我先读内容，再用音频自己核对”。（别的会话在 `fixtures/media/montage/` 旁跑 autocut 生成；我的 `cp -R` 把隐藏目录带进了沙盒。）
- L6 [T7-haiku-1] `ls -R montage/.autocut-cache`，“有现成的节拍和切点分析结果，但我不能直接信任，要用 BGM 本身验证一遍”，随后用标准库 wave 自己算能量包络；[T7-opus-2] 同样 `find .autocut-cache`。
- L7 [T9-haiku-1] “`.autocut-cache` 里可能已有转写结果，先看一下”→缓存里有节拍（120BPM，downbeat 每 2s）和两段人声区间但没有转写文本→转去借 autobake 的 whisper。
- L8 [T9-haiku-2] “直接用了 ytpmv/.autocut-cache/ 里已有的 whisper 词级时间戳，没有重新跑识别，也没下载模型”。
- L9 [T9-opus-2] `cd "$PWD/ytpmv/.autocut-cache"; for f in */*.json …` 读了缓存，最终回复里说“切点没用缓存里的识别结果”（自己决定不用）。
- L10 [6 个污染原件] 都在第一次 `find`/`ls` 里就发现了这个隐藏目录——agent 会列隐藏文件；其中 2 个直接用了里面的结果，4 个“读了但要自己核对/不信任”。

**宽泛的 `pkill` 清理（T9-haiku-1、T9-opus-2；作废运行里还有 T6-haiku-1）**
- L11 [T9-haiku-1] 执行 `pkill -f "ffmpeg -hide_banner -loglevel error" 2>/dev/null || true`——它想清掉自己上一轮遗留的后台 ffmpeg；这个 pattern 匹配所有同形态的 ffmpeg 命令行（包括并行的其他 case 和本机其他会话的 ffmpeg），它不知道。
- L12 [T9-opus-2] 执行 `pkill -f 'find / -name ggml'`——想停掉自己起的全盘 find（超时转后台后）。
- L13 [aborted-baseline2-noniso-sleep/T6-haiku-1] 执行 `pkill -f "find / -name"`，同样是清理超时的全盘 find。
- L14 三次 `pkill` 都是 agent 用来**取消自己起的长命令**：没有 `TaskStop` 的用法，也没有记录 PID 再 kill。T3-haiku-2、T4-haiku-2、T2-opus-2 则用了 `TaskStop` 停后台 find（A7、A22、U31）。这与后面接口设计里“长任务怎么取消”直接相关。
- L15 审计：两次 pkill 发生的瞬间，两个 runset 里没有别的 case 有命中该 pattern 且正在执行的调用，也没有任何 tool_result 带 143/137/Terminated/Killed 特征；所以没有 case 因此被标为 contaminated。eval 之外的进程受没受影响查不到。（加了 Seatbelt `(deny signal)` 之后，沙盒内 `pkill -f` 打沙盒外进程返回 `Operation not permitted`，agent 会看到 `pkill: signalling pid N: Operation not permitted`。）
- L16 [T3-haiku-2（iso）] 它的 `verify.py` 在 21:50:16 崩过一次（`verify_error`），该时刻正是另一个与本项目无关的会话里 `pkill -f "<uuid>" -n -l` 批量杀进程的时间点；无法复现，用 `verify_case.sh` 重新校验后 PASS。

**共用 `/tmp` 造成的串扰**
- L17 [aborted-baseline2-iso-lsdenied/T5-haiku-2] “`/tmp/gc_work` 里已经有之前的残留文件（`keep.json`、`scripts/`、`rms*.txt`），我没法判断它们来自哪次尝试，只好覆盖了其中四个同名 raw 文件，并把新产物挪到 `/tmp/gc_cut/`”——并发的 T5-haiku-1 把中间文件放在同一个固定名字下。
- L18 [T9-haiku-2][T9-opus-2] `/tmp/ytpmv_work` 在两次命令之间消失，`cd` 报错；T9-opus-2 写“这个环境的 /tmp 似乎会被清理”。（我的 `quarantine_tmp.py` 在 T9-haiku-1 结束后移走了它用过的 `/tmp/ytpmv_work`，而另外两个并发 case 也选了同一个名字。两个 agent 都改用别的目录继续，结果没变。）
- L19 批 1 未隔离版和批 2 里早先的 case 没有 `/tmp` 清理，后面的 case 可能读到前面的残留；我扫了 transcript，没有发现 agent 明确引用“前一个 case 留下的文件”，但 `/tmp/talkcut`、`/tmp/talkclean`、`/tmp/gc_work` 这类固定名字在不同 case 里反复出现。

**隔离本身给 agent 制造的摩擦**
- L20 [aborted-baseline2-iso-lsdenied/T5-haiku-1] 第一条命令 `ls -la && command -v ffmpeg …` 因为 stat 不了被拒绝的 `..` 而以 `Operation not permitted` 退出，`&&` 后面的命令没跑；debrief：“`ls ..` 报 Operation not permitted，我没用到那个信息，但它让我一开始多花了一步确认文件位置”。（隔离规则用了 `file-read*`；改成只禁 `file-read-data` 后消失，这次启动因此作废。）
- L21 [T3-haiku-1（iso）] `ls /Users/lishuyu/Codes/autocut` 返回 Operation not permitted 后写“项目 venv 因沙箱权限无法读取，我不去绕过它”——agent 把权限错误当作边界，没有去试别的路径。

**主机睡眠**
- L22 [aborted-baseline2-noniso-sleep] T5-opus-1 记 5624s、`timeout 1200` 没有触发，rc=0；debrief 拖了 50 多分钟。（主机睡眠，`timeout` 用单调时钟，睡眠期间不走；之后批跑用 `caffeinate` 包住，`timeout` 加 `-k 20`。）

**21:50:16 外部 `pkill`（另一个会话，模式 -n/-l）波及的 eval 进程**
- L23 [T6-haiku-2（interrupted 原件）] 它的第 4 个调用 `find /opt/homebrew/share/whisper.cpp …; find ~ /opt/homebrew -name …`（命令行含 `-n`）在 21:49:14 起跑，21:50:16 结束、退出码 144；agent 把它当成“超时”，debrief 写“它超时（exit 144）且没有结果”，并据此认定“本机无法做语音识别，字幕只能从弹幕来”。（重跑里没被打断的搜索得出同样的结论，所以这次打断没有改变结果。）
- L24 [T4-haiku-2（iso，原件）][T8-haiku-2（原件）] 时间窗包含 21:50:16（T8-haiku-2 是 21:50:14 启动的），那一刻没有失败的调用；推断被杀的是外层 `timeout -k 20 N sandbox-exec -f <含 -lishuyu 的 profile 路径> claude …` 包装进程（它们的命令行命中 `-l`；我没有直接观察到进程被杀），claude 本身跑完了。按“正在运行的 case 一律标 interrupted”的要求归档并重跑，两个重跑都 PASS。
- L25 [T3-haiku-2（iso）] 被杀的是我的 `verify.py`（`verify_error`），agent 在此之前已结束；重新校验 PASS，不重跑。
- L26 [T7-haiku-2、T9-opus-2（污染原件）] 另一个会话说它们的 transcript 停在 21:50 附近：这两个是已因 `.autocut-cache` 作废的原件（T7-haiku-2 在 21:50:09 结束，T9-opus-2 在 21:50:20–21:50:29 结束，均为正常结束），批 2 用的是 22:05 之后的重跑，不受影响。
- L27 审计范围：两个 runset 的最终 case 里，时间窗包含 21:50:16 的只有 T4-haiku-2（iso）、T6-haiku-2、T8-haiku-2（以及已作废的 T9-opus-2 原件）；其余 case 的 transcript 都有 result 事件和成功的 debrief，没有 143/137/Terminated/Killed 痕迹。我自己的后台等待器和 xargs 队列没有被波及（查过：无遗留进程，日志与 verify 文件数一致）。

## 4.5 作废/被剔除的运行一览

| 位置 | 原因 | 处理 |
|---|---|---|
| `evals/runs/baseline-2026-10-28`（16 个，noniso） | 沙盒不隔离，agent 读到整个文件系统（仓库 models、fixtures、calib、cwd 里的 autocut） | 保留，作补充证据（本章 4.1–4.3）；隔离版重跑见第二章 |
| `evals/runs/aborted-baseline2-noniso-sleep` | 批 2 第一次启动：未隔离 + 主机睡眠使耗时失真 | 只当旁证 |
| `evals/runs/aborted-baseline2-iso-lsdenied` | 批 2 第二次启动：隔离规则用了 `file-read*`，每个 case 首条 `ls -la` 失败（L20） | 只当旁证 |
| `evals/runs/aborted-baseline2-shared-tmp` | 批 2 第三次启动：只完成了 T5，发现 `/tmp` 共用残留会污染后停掉，改成“不同任务交错并发”的顺序并清理 `/tmp` | 只当旁证 |
| `evals/runs/baseline2-2026-10-28/interrupted/`（T6-haiku-2、T8-haiku-2）、`evals/runs/baseline-2026-10-28-iso/interrupted/`（T4-haiku-2） | 21:50:16 被外部 `pkill` 波及（L23–L27） | 原件归档（`evals/archive_case.sh`），按要求重跑，统计用重跑结果；T6-haiku-2 重跑结论不变 |
| `evals/runs/baseline2-2026-10-28/contaminated/` 的 6 个 | 素材目录带着 `.autocut-cache`（L5–L10） | 原件归档（其中 T7-opus-2、T9-haiku-1、T7-haiku-1 的原 transcript 被我一次 zsh 分词失误覆盖，已从 `~/.claude/projects/*/` 的 session 存档找回为 `*.session.jsonl`）；用新脚本重跑，批 2 统计用重跑结果 |

## 4.6 基础设施变更时间线（按生效顺序）

1. 清掉父会话身份环境变量（`CLAUDECODE`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_MESSAGING_*` 等）——批 1 起。
2. Seatbelt 读隔离 `deny file-read-data`（仓库、session 目录、本项目 `~/.claude` 记忆/teams）——批 1 隔离版、批 2 第四次启动起。
3. `run_matrix.sh` 交错并发 + `caffeinate` + `timeout -k 20`——同上。
4. `(deny signal)`、素材复制 `rsync --exclude='.*'`、`quarantine_tmp.py`（每个 case 结束后移走它用过的 `/tmp` 顶层路径）——批 2 的 6 个重跑 case 起。
5. 21:50:16 外部 `pkill` 之后：被波及的 3 个 case 用 `evals/archive_case.sh` 归档（`interrupted/`）并重跑；`summarize.py` 的 metrics.md 列出被剔除的 contaminated / interrupted case。
6. 校验器：T6 回源定位聚类、T7/T9 BGM 全频段+低频段、T9 起音口径、T7 成片位置放宽、T5 一帧容差——全部在读完结果之后，对保留的沙盒重新校验（`evals/verify_case.sh`），原因见 3.5。
