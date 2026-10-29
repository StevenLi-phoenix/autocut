# 接口形态备选 — autocut（ACI Ideate）

日期：2026-10-28 · 依据：`trajectory-storyboards.md`、`../02-define/affinity.md`、`docs/design/autocut/01-empathize/reference-remotion.md`

三种形态在真正的设计维度上各不相同：粒度、承载方式（surface）、状态放在哪里、人在哪里确认。每种都写明它缩短或消除了故事板里的哪几步。

## 备选 1：CLI（工作流级命令）+ Claude Code skill + 方案文件 —— **选用**
- **粒度**：按工作流设计。一个命令完成"分析 → 判断 → 写方案"（cut / highlights / montage / ytpmv / narrate），另有读（show）、改（set）、出片（render）、导出（export）、体检（check）、转写（transcribe）、自检（doctor）。不按底层能力一一包装成命令：没有 `detect_silence`、`run_ffmpeg` 这类命令。
- **承载**：shell 命令。所有画像都已经有 Bash 工具；skill 只负责"什么时候该用"和最常用的 3 条命令。
- **状态**：放在磁盘上的方案 JSON 里，带 revision。人在审阅页里改、agent 用 `set` 改的是同一份；`--expect-revision` 防止互相覆盖。
- **人在哪里确认**：render 之前。`next` 先给出 `review`；skill 要求出片前把"拿不准"的几处转告给人。
- **缩短或消除的步骤**：TS1 的第 2–6 步变成 1 步，第 4 步不再硬凑数；TS2 的第 1–2 步变成 1 步；TS3 和 TS4 原本做不到，现在可以。
- **代价**：常驻成本只是 skill 描述，大约 100 字；CLI 的 `--help` 要用时才读。

## 备选 2：MCP server（10 个左右的工具）
- **粒度**：和备选 1 相同，但每个命令变成一个 MCP 工具，带 JSON schema。
- **好处**：schema 强制参数类型；人不需要装 shell 也能用，比如 claude.ai 的 connector。
- **否决理由**：
  1. 常驻成本高：10 个工具的 schema 一直占着工具列表。委托人的环境里已经有上百个 MCP 工具，很难进入 agent 的选择范围（启发式 11、12）。
  2. 视频文件在本机，用 connector 的那类画像本来就不在设计目标里（agent-profiles："不在设计目标内"）。
  3. Remotion 在 2026-10-26 关掉了官方 MCP，改为 skills 加 md 文档，理由相同（reference-remotion.md）。
  4. 长时间的出片在 MCP 里需要 progress notification，处理取消也更复杂；CLI 给 PID 就能取消（TS4）。
- **保留的方向**：等真的出现"没有 shell 的画像"时，再在同一组函数外面包一层薄薄的 MCP。

## 备选 3：只提供文档和方案 schema，让 agent 自己写 JSON，再用 render 出片
- **粒度**：最底层。没有配方，agent 自己判断，然后写方案 JSON。
- **好处**：最灵活，接口面最小。
- **否决理由**：
  1. 判断（哪里是重说、哪里是高光、怎么对拍）正是 baseline 里失败的部分（A-1、A-3、B-2）。把判断交回 agent，等于回到 as-is。
  2. 手写 JSON 容易违反不变量，比如边界交叉、首尾不相接（C-4、C-5）。
- **吸收的部分**：方案 schema 照样公开，`Plan.from_dict` 报错时列出可选字段，agent 可以手写一些特殊的方案；但 skill 推荐用 `set`。

## 横切决定
- **输出格式**：非 TTY 时输出 JSON，并且有长度上限，因为 agent 要据此做决定；TTY 时输出给人看的摘要。不提供 `response_format=detailed`，需要细节就用 `show --segments` 分页查看，避免一次返回整份方案。
- **验证内建**：render 自带独立核验；export 明说"还没在剪辑软件里打开过"；check 明说"不是爆款保证"。这样 agent 就不会拿代理量来宣布成功（SH-B）。
- **能力边界写清楚**：doctor 和 transcribe 告诉 agent "可以用什么"，弥补 E-2 只说"不许什么"的问题。
