# 纸面原型 — autocut CLI + skill

日期：2026-10-28 · 方法：只给模型看 skill 草案和命令说明，让它为 18 个任务规划调用，**不执行**（`claude -p --tools "" --strict-mcp-config --disable-slash-commands`）。
原始材料：`paper/prompt.md`、`paper/raw-{haiku,opus}.json`（原始事件流）、`paper/plan-{haiku,opus}.md`
成本：haiku $0.012，opus $0.334，各 1 轮

**有效性说明**：prompt 里的命令参考表被截断了，原因是用 `---` 截取时命中了表头分隔行，这是我的 bug。所以凡是"看不到参数"类的困惑，有一部分来自截断。但两个模型都改用"先 `--help` 再用"的策略，这本身也是有用的信号：`--help` 必须完整，而且第一段就要写清楚。

## 选择是否正确（启发式 11：propensity）

| 任务 | haiku | opus | 判定 |
|---|---|---|---|
| T1–T9 | 全部选对配方 | 全部选对 | ✅ |
| N1 时长/编码、N2 压缩/GIF、N3 拼接/旋转、N4 提取 mp3 | 都用 ffprobe/ffmpeg | 同左 | ✅ 不该用 autocut 的时候没用 |
| N5 改 README | 没碰 autocut | 同左 | ✅ |
| N6 只要字幕 | `transcribe --format srt` | 同左 | ✅ 边界任务选对 |
| S1 次日审计 | 找方案 → show → set | 同左，还主动区分成片时间和素材时间 | ✅（暴露了时间基准问题，见下） |
| S3 取消 | `kill N`，没用 pkill | 同左 | ✅ |
| H5 高光不足 | "只找到 2 条，不补" | 同左，并提出可以加 `--chat` | ✅ |

## 发现的问题 → 改动

| # | 现象 | 谁 | 根因（启发式） | 改动 |
|---|---|---|---|---|
| P1 | 把 `autocut review` 当普通步骤执行，而它会一直运行，agent 会卡住 | haiku | 3 控制与自由、5 防错 | 非 TTY 且没加 `--detach` 时立即失败（`would_block`），fix 写清"交给用户运行或加 --detach"；`--detach` 返回 url 和 pid；skill 写明"把命令交给用户" |
| P2 | T6 打算用 ffmpeg 再做一遍竖屏裁切和烧字幕 | haiku | 10 文档就位 | skill 的配方表加一列"已经包含"：9:16、字幕、抽奖刷屏识别 |
| P3 | export 的 `say` 字段和 macOS 的 `say` 配音命令重名 | opus | 2 贴近 agent 的世界、4 一致性 | 改名为 `tell_user`，凡是"给用户转述的一句话"都用这个名字 |
| P4 | 用户说"3:12 那段"，不知道按成片时间还是素材时间 | opus | 6 识别而非回忆 | `show --around` 接受 m:ss，默认按成片时间；返回同时给 `start`/`end`（素材）和 `out_at`（成片），并写明 `timebase` |
| P5 | 对同一素材重跑 cut，会不会冲掉人改过的方案？ | opus | 3、5 | 已有方案时不加 `--force` 就失败（`plan_exists`），fix 说明 revision 和人工改过的片段数 |
| P6 | `--pace relaxed` 是"阈值放宽"还是"剪得少"，有歧义 | 两者 | 2 | help 和 skill 写明秒数：tight 0.15s、natural 0.4s、relaxed 0.8s |
| P7 | 达芬奇用哪种导出格式 | 两者 | 10 | 写明格式和软件的对应：fcpxml 对应 FCP 和 Resolve，xmeml 对应 Premiere，edl 通用 |
| P8 | 多输入时方案文件叫什么 | opus | 6 | 以第一个素材命名；skill 写"以返回里的 `plan` 字段为准" |
| P9 | 取消后留下的半成品，会挡住下一次 render（因为不覆盖已有文件） | opus | 9 可恢复 | 取消时删除写了一半的文件 |
| P10 | 不知道 render 核验了什么，也就不知道自己还要补查什么 | opus | 1 状态可见 | help 和 skill 列出核验项：时长、音画漂移、响度、接缝是否落在说话中间 |
| P11 | uncertain 的阈值不明 | opus | 1 | help 和 skill 写明 0.75 |
| P12 | 游戏实况用哪条轨、怎么识别"打怪"，不清楚 | 两者 | 10 | skill 写明：自动挑麦克风轨；没人说话但游戏声或画面激烈的段保留 |
| P13 | ytpmv 里"这一句"怎么指定 | opus | — | 暂不改（YAGNI）：素材通常就是一句；有多句时 agent 先 `transcribe` 再问用户，这条路已经可行 |

## 正面信号（不要改坏）
- 两个模型都把 `next` 当参考，没有照抄，也没有跳过人直接出片："render 之前先告诉用户"这条规则起作用了。
- 两个模型都主动用 ffprobe 独立核对成片时长，这是 SH-B 期望的行为。
- N 类任务全部正确地绕开了 autocut：skill 描述里"直接用 ffmpeg"的那句起作用了，扩写后覆盖得更全。

## 下一步
- 等 CLI 落实 P1–P11 后，用真实的 `--help` 文本重跑一次纸面原型（不再截断），作为回归检查。
- 然后进入候选版 eval：T1–T9、N1–N6、H5、S1、S3，haiku 和 opus 各 ≥3 次，和 baseline 对照。
