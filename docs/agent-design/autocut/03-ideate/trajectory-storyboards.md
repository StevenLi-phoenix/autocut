# 轨迹故事板 — autocut（ACI Ideate）

日期：2026-10-28 · 依据：`../02-define/affinity.md`、`../02-define/problem-statement.md`、`../01-empathize/baseline-observations.md`。as-is 取自真实 transcript（卡片编号）；to-be 是设计目标，要用候选版 eval 来验证。

---

## TS1 — T6 直播切片 · as-is（P2 haiku，`baseline2-2026-10-28/T6-haiku-1`）

画像：P2 Haiku，headless，隔离沙盒 · 触发："直播录像切 3 条高光发抖音，竖屏带字幕"

| # | Agent 知道 / 想要 | 选择（原因） | 调用 | 看到 | 决定 | 信号 |
|---|---|---|---|---|---|---|
| 1 | 要先弄清素材 | ffprobe 加 RMS 窗口 | Bash ×3 | 150s、横屏、chat.jsonl | 先看弹幕 | 🙂 |
| 2 | 字幕需要转写 | 找 ASR："本机有没有 whisper" | `find ~ /opt/homebrew -name …` | 慢；被打断后当成超时（A-1、L23） | 认定"本机没有可用 ASR，字幕只能用弹幕" | ⚠️ 结论建立在偶然事件上 |
| 3 | "高光"需要判据 | 用最好读的一路信号：弹幕密度 | Python 数每秒弹幕 | 59–70s 弹幕 18× | 选进来 | ⚠️ 没区分诱饵（A-3、D1） |
| 4 | 凑够 3 条 | 降阈值 | 同上 | 第 3 个真高光没有弹幕峰 | 挑了一段平淡的讲话凑数 | ⚠️ E-1 硬凑 |
| 5 | 做竖屏和字幕 | 手写 filtergraph | ffmpeg ×4 | 两次 "Option not found" | 换写法重试 | 🔁 C-2 |
| 6 | 验证 | 重跑同一个弹幕统计、看时长 | Bash | 时长对 | "完成" | ⚠️ B-1 同一双眼睛 |
| n | **结束**：3 个 mp4。其中 1 条是抽奖刷屏段，1 条是凑数；决定只写在回复里 | | | | | ❌ FAIL |

合计：18 次调用 · $0.03 · 0 次人工介入（失败时也没有报错）
痛点 → 对应的簇：A-1（2）、A-3（3）、E-1（4）、C-2（5）、B-1（6）、D-1（结束时没留下决定记录）

## TS1 — T6 直播切片 · to-be（P2 haiku + autocut CLI + skill）

| # | Agent 知道 / 想要 | 选择（原因） | 调用 | 看到 | 决定 | 信号 |
|---|---|---|---|---|---|---|
| 1 | 直播切高光，skill 描述里正好写着 | `autocut highlights` — skill 的触发词"直播、高光、切片" | `autocut highlights vod.mp4 --chat chat.jsonl -n 3` | JSON 摘要（约 1.5k token）：3 条候选，每条有理由；clip_4"弹幕 18× 但 89% 是同一句「1」，判为抽奖刷屏，没选"；notes 里有授权提醒 | 理由看得懂，不用自己定阈值 | 🙂 |
| 2 | 交给人之前先自查 | `next` 里给了 `show --filter uncertain` | `autocut show plan --output all` | 3 条的开头和结尾都没切在句子中间；1 条把握度 0.6，因为只有弹幕佐证 | 要把这条转告给人 | 🤔（有依据的犹豫） |
| H | **人介入**：agent 回复"3 条候选和理由如下；抽奖段已排除；clip_2 只有弹幕佐证，建议你看一眼：`autocut review plan`"。人在审阅页把 clip_2 的开头提前 3s | | | | | |
| 3 | 出片 | `autocut render` | `autocut render plan` | 3 个文件；`verify.joints_mid_speech: 0`；字幕已经烧录 | 完成 | 🙂 有证据 |
| n | **结束**：3 个 mp4 加上方案文件（每一刀都有理由）。最终回复写出方案路径，并转述"体检是风险信号，不是爆款保证" | | | | | ✅ |

合计：3–4 次调用 · 预期 < $0.03 · 1 次人工介入（这是委托人想要的那次介入）
如果只有 2 条真高光（held-out H5）：第 1 步的摘要会说"只找到 2 条（要 3 条），没有拿弱候选凑数"，agent 照实转告 → 对应 E-1。

---

## TS2 — T3 去重说和口头禅 · as-is（P2 haiku，未隔离 `baseline-2026-10-28/T3-haiku-*`，0/2）

| # | Agent 知道 / 想要 | 选择 | 调用 | 看到 | 决定 | 信号 |
|---|---|---|---|---|---|---|
| 1 | 要知道说了什么 | 扫盘找模型 → 找到仓库里的 ggml | `find / -name "ggml*"` | 找到 q5_0 | 直接用 | ⚠️ E-2 把"本机有"当成"能用" |
| 2 | 找"嗯" | 拿 whisper 的词级时间戳 | whisper-cli | "嗯"的时间标早了约 1.7s，没有可信度 | 照单全收 | ⚠️ A-2 |
| 3 | 剪 | 按时间戳剪 | ffmpeg | — | — | |
| 4 | 验证 | 用同一个 ASR 重转写成片 | whisper-cli | "嗯"不见了 | "完成" | ⚠️ B-1，实际剪掉了有用的话 |
| n | **结束**：剪掉了"第二步"前面的半句话 | | | | | ❌ |

## TS2 — T3 · to-be

| # | Agent 知道 / 想要 | 选择 | 调用 | 看到 | 决定 | 信号 |
|---|---|---|---|---|---|---|
| 1 | 去重说，skill 描述写着"说错重来、口头禅" | `autocut cut` | `autocut cut talk.mp4` | 摘要：retake 1、filler 1、silence 6；`uncertain` 列出"嗯"（0.71）；`analysis.asr` 写明用的哪个模型 | — | 🙂 |
| 2 | 拿不准的地方要确认 | `show --filter uncertain` | 1 次 | "嗯"所在的时间、前后文字 | 告诉人这 1 处 | 🙂 |
| 3 | 出片 | `render` | 1 次 | `verify.joints_mid_speech: 0`（接缝没有落在说话中间：这是独立于生成过程的检查） | 完成 | 🙂 有证据 |
| n | **结束**：成片加方案 | | | | | ✅ |
| 降级分支 | 没有模型 | — | `autocut cut` | `degraded: true`；notes 写"没检测重说和口头禅：找不到语音识别模型……怎么补" | agent 照实告诉人，不去扫盘 | 🙂 诚实 |

---

## TS3 — 次日审计（专项 S1）· to-be

委托人第二天问："昨天那条剪了什么？为什么把 3:12 那段剪了？"
1. agent 没有昨天的上下文。它在素材旁边看到 `talk.autocut.json`，执行 `autocut show talk.autocut.json --around 192`。
2. 看到 `d031 cut retake 0.88 "被 3:20 那句取代"`，以及 `edited: false`（不是人改的）。
3. 用一句话回答，并附上恢复命令 `autocut set … d031 --keep`。

对应 D-1。as-is 时，这个问题只能靠"整条重看一遍"回答。

## TS4 — 取消长任务（专项 S3）· to-be

1. `autocut render` 开始时，stderr 里写着 `pid=48121（取消：kill 48121）`。
2. 委托人说"先停一下"。agent 执行 `kill 48121`，CLI 收到 SIGTERM，停掉自己启动的 ffmpeg，删掉写了一半的文件，退出码 5，`code=cancelled`。
3. 不会出现 `pkill -f ffmpeg`，也就不会误杀别的进程。对应 D-3。
