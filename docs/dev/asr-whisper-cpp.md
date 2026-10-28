# ASR 后端：whisper.cpp（离线）

autocut 的转写后端用本机 `whisper-cli`（whisper.cpp 1.9.5，brew 安装）。模型不下载，直接把
`~/.cache/whisper/` 里已有的 OpenAI Whisper PyTorch 权重转换成 ggml 格式。本文记录转换方法、
模型文件、推荐命令行、JSON 结构、时间戳精度、耗时和已知问题。所有数字都是在本机用
`fixtures/media/` 的两段素材（`talk_zh` 40.7 s 中文 TTS、`lecture_en` 35.3 s 英文 TTS + 底噪）实测得到，
评测对象是 truth.json 里的句子起止（已用 wav 能量包络核对：truth 与真实音频的句子起止基本对得上，多数句子偏差 ≤ 20 ms）。

## 结论速览

- 转换成功，`whisper-cli` 直接加载运行；英文三个模型全对（WER 0%），中文 turbo/q5_0 只错 1 个字（CER 0.9%），base 错约 8.5%。
- **要词级时间戳：用 f16 的 `large-v3-turbo` + `-nfa -dtw large.v3.turbo -ojf`，读 JSON 里每个 token 的 `t_dtw`。**
  `t_dtw` 是 token 的**结束时间**（单位 10 ms），句末误差约 −150 ± 100 ms；句首词的起点在停顿之后拿不到（见下文）。
- 要做到 ≤ 20 ms 的剪辑边界，需要 `t_dtw` + 音频能量包络（静音检测）吸附：中文素材实测句首/句末平均误差 5 ms / 8 ms。
- whisper 自带的启发式 token 时间戳（`-ojf` 的 `offsets`，`-ml 1`）在 turbo 上不可用；DTW 开启时 stdout 会乱序，**只解析 JSON，不要解析 stdout**。
- 口头禅（`嗯` / `Um`）和说错重来（"第二步是把没用的停顿。不对，我重新说一遍。"）在所有模型上都**被完整转写保留**。

## 模型文件

均在 `models/`（已被 .gitignore 忽略），由 `scripts/convert_whisper_pt.py` + `whisper-quantize` 生成。

| 文件 | 大小 | 来源 | 用途 |
|---|---|---|---|
| `models/ggml-base.bin` | 148.0 MB | `~/.cache/whisper/base.pt`，f16，80 mel | 快速草稿；中文质量一般 |
| `models/ggml-large-v3-turbo.bin` | 1.62 GB | `~/.cache/whisper/large-v3-turbo.pt`，f16，128 mel | **推荐**，需要词级时间戳时必须用它 |
| `models/ggml-large-v3-turbo-q5_0.bin` | 574 MB | 上一个文件 `whisper-quantize … q5_0` | 只要文本/句段时可用；`-nfa` 下英文会退化（见已知问题） |

## 转换方法

```bash
source .venv/bin/activate
python scripts/convert_whisper_pt.py ~/.cache/whisper/base.pt models/ggml-base.bin
python scripts/convert_whisper_pt.py ~/.cache/whisper/large-v3-turbo.pt models/ggml-large-v3-turbo.bin
whisper-quantize models/ggml-large-v3-turbo.bin models/ggml-large-v3-turbo-q5_0.bin q5_0
```

- 命令行：`convert_whisper_pt.py <in.pt> <out.bin> [--assets DIR] [--f32] [-v]`。`--assets` 缺省时自动探测
  含 `mel_filters.npz` 的目录（已安装的 `whisper`/`mlx_whisper` 包，或 `~/.cache/uv/archive-v0/*/mlx_whisper/assets`）。
  耗时：base 约 0.4 s，turbo 约 2.5 s。
- **不依赖 torch 读 .pt**：`.pt` 是 zip，`<root>/data.pkl` 是 pickle，`<root>/data/<key>` 是各 storage 的原始字节
  （`<root>` 在 base 里是 `archive`，在 large-v3-turbo 里是 `large-v3-turbo`，脚本按 `*/data.pkl` 自动识别）。
  自定义白名单 `pickle.Unpickler`：只放行 `collections.OrderedDict`、`torch._utils._rebuild_tensor_v2`、
  `torch.{Half,Float,Double}Storage`，其余 pickle 全局对象一律拒绝；`persistent_load` 按 key 读 zip 内字节并
  `np.frombuffer`，`_rebuild_tensor_v2` 用 `as_strided` 按 storage_offset / size / stride（单位都是元素）还原。
  两份权重实际全是 `HalfStorage`。
- **ggml 布局与官方 `models/convert-pt-to-ggml.py` 一致**：magic `0x67676d6c` → 10 个 int32 hparams
  （n_vocab, n_audio_ctx/state/head/layer, n_text_ctx/state/head/layer, n_mels）+ ftype → mel 滤波器
  （int32 行列数 + f32 行优先数据，取 `mel_80` / `mel_128`）→ 词表（50257 个 tiktoken token，`int32 长度 + 字节`，
  取自 `multilingual.tiktoken`；n_vocab < 51865 才用 gpt2.tiktoken）→ 张量（`int32 n_dims, name_len, ftype`，
  维度**倒序**，名字，数据）。
- **精度规则**（同官方）：默认 f16；`ndim < 2` 的张量（bias、LayerNorm）以及 `encoder.conv{1,2}.bias`、
  `{encoder,decoder}.positional_embedding` 写 f32；两个 conv bias 先 reshape 成 `[n, 1]`。base 共 245 个张量：146 个 f32 + 99 个 f16。
- 校验：① 官方 `for-tests-ggml-tiny.bin` 与产物的 magic / hparams 布局 / mel 形状 (80, 201) / 词表条目数 (50257) 完全一致
  （该空壳文件没有张量，张量段靠第 ② 点验证）；② 自己写的解析器能把产物逐张量读完且无多余字节；
  ③ `whisper-cli` 加载并正确转写；④ 重复转换逐字节一致；⑤ `--f32` 产物也能加载，前 3 段转写与 f16 相同。

## 推荐命令行

`whisper-cli` 只收 flac / mp3 / ogg / wav，先用 ffmpeg 抽 16 kHz 单声道 wav：

```bash
ffmpeg -nostdin -y -i in.mp4 -ar 16000 -ac 1 -c:a pcm_s16le /tmp/in.wav
```

**词级时间戳模式（autocut 默认用这个）**，中文 / 英文只差 `-l`：

```bash
whisper-cli -m models/ggml-large-v3-turbo.bin -l zh -np -nfa -dtw large.v3.turbo -ojf -of /tmp/out -f /tmp/in.wav
# 产物：/tmp/out.json
```

- `-l zh` / `-l en` / `-l auto`（两段素材上 `auto` 与显式语种结果相同）。
- `-nfa`（关 flash attention）**必须**：默认开着 flash attention，此时 `-dtw` 会被静默关掉
  （日志 `dtw_token_timestamps is not supported with flash_attn - disabling`），所有 `t_dtw` 都是 −1。
- `-dtw` 的预设名要与模型匹配：turbo 用 `large.v3.turbo`，base 用 `base`。
- `-ojf` 才有 token 级数据；`-oj` 只有段级。
- `-np` 去掉模型加载日志。DTW 模式下 stdout 的实时打印会乱序/重复（见已知问题），结果以 JSON 为准。

**纯文本 / 句段模式**（不需要词级时间，更快；可用 q5_0）：

```bash
whisper-cli -m models/ggml-large-v3-turbo-q5_0.bin -l zh -np -f /tmp/in.wav
```

其他输出：`-otxt`（每段一行）、`-osrt` / `-ovtt` / `-olrc`（段级时间）、`-ocsv`（`start,end,text`，毫秒）、
`-oj`（段级 JSON）、`-owts`（卡拉 OK 脚本）。都只有段级时间，没有词级信息；词级只有 `-ojf`。

base 的中文默认输出繁体，需要简体时加 `--prompt "以下是普通话的句子。"`（turbo 不加也是简体）。

## JSON 输出结构（`-ojf`）

```text
{ systeminfo, model{type, vocab, audio{…}, text{…}, mels, ftype}, params{model, language, translate},
  result{language},
  transcription: [ { timestamps{from,to}, offsets{from,to}, text, tokens:[ … ] }, … ] }
```

- 段：`offsets` 是毫秒整数，`timestamps` 是 `HH:MM:SS,mmm` 字符串。
- token：`text`、`id`、`p`（该 token 的概率）、`offsets{from,to}`（启发式时间，毫秒）、
  **`t_dtw`（DTW 时间，10 ms 为单位，`-1` = 无）**。每段开头可能有 `[_BEG_]`，结尾有 `[_TT_n]` 时间戳 token，
  它们的 `t_dtw` 是 −1，处理时按 `text` 以 `[_` 开头过滤。
- token 粒度：英文是 BPE 子词，**以空格开头的 token 才是新词**（`" g"` + `"ulf"` = gulf）；中文是单字或常用词
  （`大家好`、`我们`、`今天` 各占一个 token），标点单独成 token。两段素材分别有 120 / 99 个 token（含特殊 token），没有出现被截断的 UTF-8。

完整 JSON 样例在 `models/tmp/samples/`（本地，未入库）。截短的真实样例（`talk_zh`，turbo f16，`-nfa -dtw large.v3.turbo -ojf`，第 2 段，只留了 4 个 token）：

```json
{
  "systeminfo": "...",
  "model": { "type": "large", "multilingual": true, "vocab": 51866, "mels": 128, "ftype": 1, "...": "..." },
  "params": { "model": "../ggml-large-v3-turbo.bin", "language": "zh", "translate": false },
  "result": { "language": "zh" },
  "transcription": [
    {
      "timestamps": { "from": "00:00:10,980", "to": "00:00:19,540" },
      "offsets": { "from": 10980, "to": 19540 },
      "text": "首先你需要准备好原始素材,嗯,然后打开剪辑软件,把素材拖进时间线。",
      "tokens": [
        { "text": "首先", "timestamps": { "from": "00:00:10,980", "to": "00:00:10,980" },
          "offsets": { "from": 10980, "to": 10980 }, "id": 36490, "p": 0.999184, "t_dtw": 1156 },
        { "text": ",", "timestamps": { "from": "00:00:12,430", "to": "00:00:12,800" },
          "offsets": { "from": 12430, "to": 12800 }, "id": 11, "p": 0.965663, "t_dtw": 1440 },
        { "text": "嗯", "timestamps": { "from": "00:00:12,800", "to": "00:00:12,980" },
          "offsets": { "from": 12800, "to": 12980 }, "id": 21206, "p": 0.719552, "t_dtw": 1466 },
        { "text": ",", "timestamps": { "from": "00:00:12,980", "to": "00:00:13,230" },
          "offsets": { "from": 12980, "to": 13230 }, "id": 11, "p": 0.742259, "t_dtw": 1506 },
        "..."
      ]
    },
    "..."
  ]
}
```

注意 `"嗯"` 的 `t_dtw` = 1466 即 14.66 s，truth 里这个口头禅是 14.509–14.743 s；而它的 `offsets`（12.80–12.98 s）差了 1.7 s。

## 时间戳精度

评测方法：把 token 文本按字符与 truth 句子做序列比对，取每句首/末 token 的时间，与 truth 的句子起止求差
（负数 = 比 truth 早）。样本：turbo f16，`-nfa -dtw large.v3.turbo -ojf`，中文 10 句 + 英文 9 句。

| 来源 | 句首 | 句末 | 结论 |
|---|---|---|---|
| **DTW 末 token 的 `t_dtw`** | — | 中文平均 143 ms，英文 178 ms；中位数 −148 / −166 ms，最差 249 / 272 ms | 稳定地早约 150 ms，**可用**，补 +150 ms 后残差约 ±100 ms |
| DTW 句首词（取前一个 token 的 `t_dtw` 作起点） | 句内连续语流可用；**停顿之后会错 0.8–3.5 s** | — | 停顿的静音被算进前一个标点 token，句首词的起点不可恢复 |
| 启发式 token 时间（`offsets`，不带 `-dtw`） | 中文平均 617 ms，英文 320 ms | 中文 939 ms，英文 758 ms | turbo 上**不可用**：词会塌在同一个时刻（`首先 你 需要 准` 全是 10980–10980），句首常被拉到上一段末尾 |
| 段级（`transcription[]`） | 与句子不对齐 | 与句子不对齐 | 中文 5 段对 10 句，英文 8 段对 9 句；英文段边界把静音吸进去（"Okay, let's get started." 的段是 0–5.44 s，实际语音 1.5–3.23 s） |

t_dtw 与 truth 的对照（截取，ms）：

| 句子 | truth 句末 | 末 token `t_dtw` | 误差 |
|---|---|---|---|
| 大家好，欢迎回到我的频道。 | 2811 | 2720 | −91 |
| 嗯（口头禅） | 14743 | 14660 | −83 |
| 不对，我重新说一遍。 | 27084 | 26920 | −164 |
| 我们下期再见。 | 39545 | 39320 | −225 |
| Okay, let's get started. | 3230 | 3120 | −110 |
| Um（口头禅） | 9587 | 9460 | −127 |
| sorry, let me say that again. | 20926 | 20760 | −166 |
| Questions? | 34344 | 34120 | −224 |

**DTW + 音频能量包络**（实测推荐做法）：词序和词尾用 `t_dtw`，边界用 10 ms 帧 RMS（门限 = 全片第 10 百分位 + 12 dB）吸附：
句首 = 前一 token 的 `t_dtw` 之后第一个有声帧；句末 = `t_dtw − 100 ms … + 500 ms` 内最后一个有声帧（连续 150 ms 静音即止）。

| 素材 | 句首误差 | 句末误差 |
|---|---|---|
| talk_zh（安静） | 平均 5 ms，最大 19 ms | 平均 8 ms，最大 19 ms |
| lecture_en（−38 dBFS 底噪） | 中位数 3 ms；9 句中 2 句被底噪突发误触发（−750 / −2413 ms），其余 ≤ 105 ms | 平均 41 ms，最大 104 ms |

所以底噪素材上不能用这么简单的固定门限，autocut 自己的静音检测（带底噪估计）应该替换它；ASR 负责"说了什么、哪个词在哪一句"，
边界以音频为准。DTW 本身的分辨率是 20 ms（`t_dtw` 都是偶数）。

不同模型对比（句末误差平均）：turbo f16 143 / 178 ms，turbo q5_0 145 / 172 ms（中 / 英），base（`-dtw base`）304 / 125 ms。

## 转写质量与耗时

准确度（对 truth 全文，去标点后；中文 CER，英文 WER）：

| 模型 | 中文 CER | 英文 WER | 说明 |
|---|---|---|---|
| base | 8.5%（加简体 prompt 后） | 0% | 中文：`录屏快速剪成`→`路频快速简称`、`导出`→`到初`、`剪辑`→`简级`、`拖进`→`脱进`；不加 prompt 输出繁体 |
| large-v3-turbo f16 | 0.9% | 0% | 中文只错一处：`录屏` → `录音`（flash attention 开）/ `录片`（`-nfa`） |
| large-v3-turbo q5_0 | 0.9% | 0% | 同上 |

耗时（40 s 中文素材 `talk_zh`，Apple Silicon 14 核，Metal 后端，第二次运行的 wall time，含模型加载）：

| 模型 | 默认（flash attn） | `-nfa -dtw`（词级） | 纯 CPU（`-ng`） |
|---|---|---|---|
| base | 0.54 s | 0.68 s | 2.11 s |
| turbo f16 | 2.06 s（加载 0.63 s） | 2.14 s | — |
| turbo q5_0 | 1.65 s（加载 0.17 s） | 1.85 s | 16.2 s |

英文 35 s 素材 turbo q5_0 默认 1.51 s。实时倍率约 20–75×；第一次读模型（冷盘）会更慢。

逐句对照（turbo f16，默认参数即 flash attention 开，括号内是 truth 原文有差异的部分）。中文 `-l zh`：

```text
[0.00 → 9.14]   大家好,欢迎回到我的频道,今天我们来聊一聊,怎么把一段录音(屏)快速剪成可以发布的视频。
[11.00 → 19.54] 首先你需要准备好原始素材,嗯,然后打开剪辑软件,把素材拖进时间线。
[21.92 → 27.02] 第二步是把没用的停顿。不对,我重新说一遍。
[27.86 → 31.42] 第二步,是把没有用的停顿全部删掉。
[34.86 → 39.34] 最后检查一遍,导出就可以了,我们下期再见。
```

英文 `-l en`：

```text
[0.00 → 5.42]   Okay, let's get started.
[5.42 → 8.96]   Today we cover the gulf of execution and the gulf of evaluation.
[8.96 → 15.94]  Um, both ideas come from Don Norman.
[15.94 → 18.56] The gulf of execution is the gap between.
[18.56 → 21.68] Sorry, let me say that again.
[21.68 → 26.48] The gulf of execution is the gap between what you want and what the interface lets you do.
[26.48 → 33.46] The gulf of evaluation is the gap between what the system shows and what you understand.
[33.46 → 35.34] Questions?
```

## 口头禅与说错重来

| 场景 | 结果 |
|---|---|
| 中文 `嗯` | turbo / q5_0：转写为 `嗯`，单独一个 token（p = 0.72）；base：`恩`（不加 prompt）或 `呃`（加简体 prompt） |
| 英文 `Um` | 三个模型都转写为 `Um`；turbo 与前后文并进同一段（`Um, both ideas…`），base 单独成段 |
| 说错重来（前半句被截断） | 前半句、`不对，我重新说一遍` / `sorry, let me say that again`、完整重说，三段全部保留，顺序正确 |
| 静音间隙（中文 3 s、英文 4 s） | 没有幻觉文本 |
| 用 `--prompt "嗯，呃，那个，就是说。"`（中文）/ `"Um, uh, so, like, you know."`（英文），q5_0 | 文本不变；英文段终点不再吸入静音（第一段 0–3.16 s，不加 prompt 是 0–5.44 s） |

句内的 `嗯` 会被并进它所在的段落文本里（`…原始素材,嗯,然后…`），要把它当作独立片段处理，得靠 token 级结果
（`text == "嗯"` 或 `"Um"` 的 token，前后标点 token 的 `t_dtw` 给出大致范围，再用能量包络吸附）。

## 已知问题

1. **DTW 模式下 stdout 乱序/重复**（turbo f16 + `-nfa -dtw`：英文素材 stdout 打印了 15 行，实际只有 8 段，顺序也错）。
   JSON 是完整、正确、按顺序的，只解析 JSON。
2. **`-dtw` 必须配 `-nfa`**，否则静默失效，`t_dtw` 全为 −1。
3. **q5_0 + `-nfa` 英文会退化**：文字基本对，但变成全小写、无标点、5 个大段并有跨段重叠
   （`okay let's get started today we cover the Gulf of execution …`）。所以词级模式用 f16 turbo，q5_0 只在默认的 flash attention 下用。
   中文 q5_0 + `-nfa` 正常。
4. **启发式 token 时间戳不可用**（turbo）：不要用 `offsets` 或 `-ml 1`。`-ml 1 -sow` 在中文上根本不切分（没有空格可切，
   输出与不加时一样仍是 5 段）；英文上段时间是启发式时间的重新拼接，仍然不准。
5. **`t_dtw` 是 token 的结束时间**，不是起点；起点只能取前一个 token 的 `t_dtw`，在停顿之后失效（静音归给了前一个标点）。
6. **词级边界需要音频吸附**，见上文；底噪素材要用带噪声估计的静音检测。
7. flash attention 开关会改变同音字的选择（`录音` / `录片`），两者都没转对 `录屏`；专有词建议后续用 `--prompt` 提示或做文本后处理。
8. 中文输出的标点是半角 `,`（模型自己的输出），不是 `，`；base 无 prompt 时输出繁体。
9. 素材是 TTS，发音清晰、停顿规整；真人语音里 Whisper 更容易把口头禅和重复"整理"掉，口头禅 / 重说的保留率需要用真实录音再验证
   （本次的 prompt 提示只在 TTS 上验证过）。
10. `whisper-cli` 不能直接读 mp4，必须先过 ffmpeg；`--vad` 需要单独的 VAD 模型文件（本机没有，未测试）。
11. 同一份权重在不同 flash attention 设置下的数值有细微差别，`-nfa` 与默认的文本偶有 1 个字不同，测试断言不要逐字匹配整段文本。
12. 本次没有给 `scripts/convert_whisper_pt.py` 加 pytest（任务范围限定）；验证靠上面的 5 项校验。
