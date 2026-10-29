# 参考研究：Remotion（设计参考，非选型结论）

- 调研日期：2026-10-28。Remotion 当前最新版 `4.0.539`（2026-10-26 发布；近 10 天连发 4.0.536 / 537 / 538 / 539）[releases](https://github.com/remotion-dev/remotion/releases)。仓库 68.6k stars、5.6k forks、209 open issues，当天仍有提交 [repo](https://github.com/remotion-dev/remotion)（GitHub API 读取）。
- 目的：给 autocut 的设计决策提供依据。人和 AI agent（Claude Code）都会用 autocut，验收标准是"能做出爆款视频"。本文只列证据和取舍，不替你们做决定。
- 证据标注：**[官方文档]** remotion.dev/docs；**[官方源码]** GitHub 仓库文件；**[issue]** GitHub issue/PR；**[第三方]** 博客等；**[推算]** 我基于证据的计算，非原文。
- 来源说明：官方文档读的是 GitHub `main` 分支的 `.mdx` 源文件（即 remotion.dev 的文档源），可能略新于 npm 的 4.0.539（例如 `/remotion-license` skill 标注 "available from v4.0.540"）。引文保留英文原文，只摘短句。
- **未覆盖 / 未找到证据**：Discord（无法检索，本文没有任何 Discord 摘要）；Reddit / HN 上的长视频性能讨论（WebSearch 未命中直接材料）；官方本地渲染速度基准（只有 Lambda 的图表，数字无法可靠还原）；内置节拍检测（文档里没有）。这些在对应章节会再标出。

---

## 0. 五条最关键的结论（先看这个）

1. **Remotion 没有"声明式时间线数据"，代码就是时间线。** 官方自己说"A Remotion composition is React code, not a timeline data structure"，并且"It is not possible to deterministically export a Remotion project to the OpenTimelineIO format"。它的可视化编辑（Studio Interactivity）靠"约束代码形状 + 把编辑写回 JSX 源码"实现，所以官方 skills 里大半规则都是"怎么写才不破坏可编辑性"。autocut 用 JSON 时间线是与它的根本差异，也是优势所在（可校验、可 diff、可导出）。见 §1、§3。
2. **官方给 agent 的路线是 Skills + Markdown 文档，不是 MCP。** 官方 MCP 已于 2026-10-26 关停，理由原文：数据可能比文档旧、Remotion 付 token 成本、"Installing MCP servers was difficult, and agents did not invoke them reliably"、与 `/remotion-docs` skill 重复。见 §3。
3. **渲染 = 无头 Chrome 多标签页逐帧截图 + 并行 FFmpeg 编码，组件必须是"帧号的纯函数"。** 这决定了它适合"合成型"内容（字幕动画、知识视频、数据可视化、模板批量），不适合"对长素材做大量精确剪切"：维护者自己写的 issue 说"Videos are extracted frame-by-frame, making it difficult to parallelize"；单点实测 66,650 帧（1080p60）在 16 核上 50.8 分钟（21.9 fps），修复 O(n²) 后 28.7 分钟（38.7 fps）。见 §4、§5。
4. **Remotion 官方自己的口播工具 Recorder，也不在 Remotion 里切长录音。** 它推荐"record many small bite-sized clips"，事后切长录音用外部 ffmpeg 脚本；静音检测也是 ffmpeg 做完再把结果喂给 `trimBefore` / `durationInFrames`。"ffmpeg 流式切割 + Remotion 合成包装"的分工有官方先例。见 §5。
5. **本机直接上 Remotion 有现实风险：** macOS 27.0.1 / Apple Silicon 正是 issue #12680 的环境，Remotion 内置的 mac-arm64 Chrome Headless Shell 编进了 V8 DCHECK，渲染会 `Page crashed!`；#12633 在 macOS 26.6.2 上渲染全部超时。两者的官方回应都是用 Google 的 chrome-headless-shell 或 `--chrome-mode=chrome-for-testing` 绕过。本机 pnpm store / npm cache 里没有任何 remotion 包。首次安装下载量约 45 MB（npm，扣除 store 已有）+ 179 MB（Remotion 自带 Chrome Headless Shell）≈ 224 MB。见 §7。

---

## 1. 概念模型

### 1.1 一句话

> "A video is a function of images over time." — [The fundamentals](https://www.remotion.dev/docs/the-fundamentals)

一个 React 组件读取"当前帧号"，返回这一帧的画面；渲染器把帧号从 0 扫到 `durationInFrames - 1`，逐帧取图再编码。时间、动画、媒体裁剪全部是**帧号的函数**，单位是**帧**不是秒。

### 1.2 对象与动作

| 对象 / 动作 | 含义 | 要点（带出处） |
|---|---|---|
| **Composition** | React 组件 + 视频元数据（`id`、`width`、`height`、`fps`、`durationInFrames`、`defaultProps`、可选 zod `schema`、可选 `calculateMetadata`） | "A composition is the combination of a React component and video metadata."（[fundamentals](https://www.remotion.dev/docs/the-fundamentals)）。在 `Root.tsx` 里注册，`<Folder>` 分组，`<Still>` 是单帧图。技能文档要求每个独立可编辑的 composition 写成独立 JSX 节点（[compositions.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/compositions.md)） |
| **frame / fps** | 帧从 0 开始，最后一帧是 `durationInFrames - 1` | [use-current-frame](https://www.remotion.dev/docs/use-current-frame) |
| **`useCurrentFrame()`** | 返回当前帧；在带 `from` 的父级内部返回**相对父级起点**的帧 | 官方示例：时间线在 25，位于 `from={10}` 内的子组件返回 15（[use-current-frame](https://www.remotion.dev/docs/use-current-frame)） |
| **`useVideoConfig()`** | 取 `fps` / `width` / `height` / `durationInFrames` | 所有"秒→帧"换算都从这里取 `fps`（[markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md)） |
| **时间属性** `from` / `durationInFrames` / `trimBefore` / `playbackRate` / `loop` | 决定一个元素在时间线上的位置和它内部时钟 | 现在直接挂在 `<Video>` `<Audio>` `<AbsoluteFill>` `<Interactive.*>` 等组件上；`<Sequence>`："It is not recommended anymore to use `<Sequence>` directly, but it is possible. You will see this often in legacy Remotion code."（[sequence](https://www.remotion.dev/docs/sequence)） |
| **`Series` / `TransitionSeries` / `Track`** | 顺序排列 / 带转场的顺序排列 / 命名轨道（不重叠、起点各自独立） | [video-editing.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/video-editing.md)、[timeline-structure.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-interactivity/timeline-structure.md) |
| **`interpolate()`** | 区间映射：`interpolate(frame, [0, 20], [0, 1])`，建议默认 `extrapolateLeft/Right: 'clamp'` | [interpolate](https://www.remotion.dev/docs/interpolate)；system prompt 里同样要求 clamp（[system-prompt.txt](https://www.remotion.dev/system-prompt.txt)） |
| **`spring()`** | 物理弹簧动画原语 | [spring](https://www.remotion.dev/docs/spring) |
| **inputProps** | `defaultProps`（设计期默认值）→ 渲染时 `--props` / API 传入的 input props 覆盖（"input props have priority"）→ `calculateMetadata()` 后处理 | [parameterized-rendering](https://www.remotion.dev/docs/parameterized-rendering) |
| **`calculateMetadata()`** | 异步函数，渲染前可改 `durationInFrames` / `width` / `height` / `fps` / `props` / `defaultOutName` / `defaultCodec` | 典型用法：用 mediabunny 读视频时长，多段视频时长求和（[calculate-metadata.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/calculate-metadata.md)） |
| **zod `schema`** | 给 props 加类型，Studio 右侧栏自动生成控件；顶层必须是 `z.object()` | [parameters.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/parameters.md)。4.0.516 起不写 schema 也会从 `defaultProps` 推断控件（[visual-editing](https://www.remotion.dev/docs/visual-editing)） |
| **`<Video>` / `<Audio>`（`@remotion/media`）** | 现在推荐的媒体组件，基于 Mediabunny + WebCodecs | 见 §1.3 |
| **`<OffthreadVideo>`** | 旧的 Rust + FFmpeg 帧提取器，现在是 `<Video>` 不支持某些编码时的回退 | "For new video usage, prefer `<Video>` from `@remotion/media`."（[offthreadvideo](https://www.remotion.dev/docs/offthreadvideo)） |
| **`delayRender()` / `continueRender()`** | 告诉渲染器"数据/资源还没好，先别截图"，默认 28 秒超时 | [timeout](https://www.remotion.dev/docs/timeout) |
| **`random(seed)`** | 确定性随机；禁止 `Math.random()` | "Remotion needs all of the React code to be deterministic. Therefore, it is forbidden to use the Math.random() API."（[system-prompt.txt](https://www.remotion.dev/system-prompt.txt)） |
| **`Caption`** 类型 | 字幕统一 JSON：`{text, startMs, endMs, timestampMs, confidence, pageBreakAfter?}` | "All captions must be processed in JSON."（[remotion-captions SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-captions/SKILL.md)） |
| **Effects** | `@remotion/effects` 提供约 76 种内置色彩/模糊/光照/扭曲/纹理/像素化效果（我按 SKILL 里的清单逐项数过），挂在 `effects` 属性上 | [markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md) |

### 1.3 三种视频组件对比（对 autocut 的素材处理最相关）

来源：[Comparison of video tags](https://www.remotion.dev/docs/video-tags)

| | `<Video>` / `<Audio>`（`@remotion/media`） | `<OffthreadVideo>` | `<Html5Video>`（旧 `<Video>`） |
|---|---|---|---|
| 基于 | Mediabunny、WebCodecs | Rust + FFmpeg 二进制 | HTML5 `<video>` |
| 逐帧精确 | 是 | 是 | "Not guaranteed" |
| 渲染速度 | "Fastest" | "Fast" | "Medium" |
| 支持的编码 | AAC、FLAC、H.264、MP3、Opus、VP8、VP9、Vorbis；不支持时回退到 OffthreadVideo | 另含 AV1、H.265、AC3、PCM | 与 `<Video>` 列出的相同 |
| 部分下载素材 | 是 | 否 | 仅 `muted` |
| 需要 CORS | 是 | 否 | 否 |
| 可循环 | 是 | 否 | 是 |
| 浏览器端渲染 | 是 | 否 | 否 |

另有两条会直接影响 autocut 的限制：`playbackRate` 之下 "Reverse playback is not supported"（[embedding-videos.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/embedding-videos.md)，`<OffthreadVideo>` 页面也有同样一句）；`toneFrequency`（变调）"only works during server-side rendering, not in the Remotion Studio preview or in the `<Player />`"（同文件）。

### 1.4 它怎么把"视频"表示成"数据 + 代码"

```
代码层（TSX）            参数层（JSON）               素材层
Root.tsx 注册 Composition → defaultProps / inputProps  → public/ + staticFile() 或 URL
组件树 = 时间线           → zod schema 校验、Studio 编辑 → 字幕 = Caption[] JSON
                         → calculateMetadata 派生时长
```

- 时间线结构本身在 TSX 里（`Track` / `TransitionSeries` / `from` / `trimBefore` 都是 JSX 属性）。**没有**一份能独立描述"剪辑决策"的数据文件。
- 官方 OTIO 导出文档明说了后果："Remotion compositions are fully dynamic and programmatic. It is not possible to deterministically export a Remotion project to the OpenTimelineIO format. You can use the Agent Skill below to let your agent analyze the Remotion project and recreate an OpenTimeline file."（[export-opentimeline](https://www.remotion.dev/docs/export-opentimeline)）。也就是说要导出到 Resolve / Premiere，得让 agent 读代码再"重建"时间线，不能机械转换。
- 它的混合用法在 Recorder 模板里最清楚：固定的 composition 代码 + 一个 `scenes` 数组做 props（在 Studio 侧栏用 `+` 添加场景，保存回 `defaultProps`）+ 每段录音旁边的 Whisper 字幕 JSON，`calculateMetadata` 由字幕首尾词算出开始/结束裁剪（[recorder editing](https://www.remotion.dev/docs/recorder/editing/editing)、[silence-removal](https://www.remotion.dev/docs/recorder/editing/silence-removal)）。
- 渲染对代码的硬约束来自它的并行模型："We open multiple tabs to render the video... Tabs don't share state and animations that run independent of `useCurrentFrame()` will break."，并给出四条判据：同一帧多次调用画面相同；不依赖帧按顺序渲染；暂停时不能动；不依赖随机数（除 `random()`）（[flickering](https://www.remotion.dev/docs/flickering)）。

---

## 2. 人的工作流

### 2.1 从零到出片

| 步骤 | 命令 / 动作 | 出处 |
|---|---|---|
| 建项目 | `npx create-video@latest`（或 `--yes --blank my-video`）；也可 `bun create video`，创建时可选装 agent skills | [coding-agents](https://www.remotion.dev/docs/ai/coding-agents)、[skills](https://www.remotion.dev/docs/ai/skills) |
| 预览 | Remotion Studio（`npm run dev` / `npx remotion studio`），热更新；时间线、画布、右侧 Props 面板 | [Starting the Studio](https://www.remotion.dev/docs/studio/studio) |
| 改参数 | Props 面板（`Cmd/Ctrl+J`）：按 `defaultProps` 推断控件，或切到 JSON 编辑；改动可保存回代码 | [visual-editing](https://www.remotion.dev/docs/visual-editing) |
| 可视化改时间线 | Studio Interactivity（4.0.475 起）："visual edits that are written back to your codebase"：拖动画布改 `style.translate/scale/rotate`、改关键帧、拖放效果，支持撤销重做 | [studio/interactivity](https://www.remotion.dev/docs/studio/interactivity) |
| 渲染 | Studio 内渲染，或 `npx remotion render <id> out.mp4`、`npx remotion still`；常用选项 `--props`、`--concurrency`、`--scale`、`--frames`、`--log=verbose`；`npx remotion benchmark` 找最优并发 | [performance](https://www.remotion.dev/docs/performance)、[remotion-render SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-render/SKILL.md) |

### 2.2 `@remotion/player`：把预览嵌进网页

- "Using the Remotion Player you can embed Remotion videos in any React app and customize the video content at runtime."（[player](https://www.remotion.dev/docs/player)）。用法：`<Player component durationInFrames compositionWidth compositionHeight fps controls inputProps />`，有 ref 和事件（如 `timeupdate`）。
- 已知的预览/成片差异与坑：官方有一页专讲 "Playback issues in the `<Player>`"，症状包括 "Black frames inbetween scenes / Audio choppiness / Unexpected pausing / Jumping around"，解法是用 `@remotion/media` 的组件、给 `<Sequence>` 加 `premountFor`（[playback-issues](https://www.remotion.dev/docs/player/playback-issues)）；变调只在服务端渲染生效，Player 里听不到（见 §1.3）。
- 许可上 Player 被算作"automation"（见 §6）。

### 2.3 渲染管线（本地）

1. **打包**：webpack / rspack 把 Remotion 项目 bundle（`@remotion/bundler`）。
2. **选 composition 并算元数据**：`selectComposition()` 执行 `calculateMetadata`。
3. **逐帧渲染**：无头 Chrome 开多个标签页并行（"`concurrency` 就是同时打开的 Chrome 标签页数"，维护者在 [#4300](https://github.com/remotion-dev/remotion/issues/4300) 里说的）。
4. **帧捕获**：传统方式是 CDP 截图成 JPEG/PNG；4.0.534 起在 Apple Silicon（macOS 15+）和 Linux 上用 Remotion 定制的 Chromium 把位图经共享内存直接交给 FFmpeg，"Instead of compressing each image, the bitmap is shared directly with Remotion's FFmpeg binary"（[shared-memory-capture](https://www.remotion.dev/docs/shared-memory-capture)）。官方没有给这项优化的速度数字："Overall render speed depends on your composition, encoder and machine."
5. **并行编码**：捕获和编码同时进行（parallel encoding，视频默认开）。
6. **音频**：从各 `<Audio>` / `<Video>` 收集资产混音，AAC 编码，最后 mux（据 [#11704](https://github.com/remotion-dev/remotion/issues/11704) 对 `compressAudio` / `combineAudio` / `muxVideoAndAudio` / `stitchFramesToVideo` 的描述）。
7. 编码硬件加速默认 `"disabled"`，macOS 可用 VideoToolbox（H.264 / H.265 / ProRes），设 `hardwareAcceleration: "if-possible"`（[hardware-acceleration](https://www.remotion.dev/docs/hardware-acceleration)）。

服务端 API 三步：`bundle()` → `selectComposition()` → `renderMedia()`（[ssr-node](https://www.remotion.dev/docs/ssr-node)），官方有 Express 渲染服务模板。

### 2.4 云端 / 分布式

| 方案 | 特点 | 出处 |
|---|---|---|
| **Remotion Lambda** | 把帧范围切块并行渲染再拼接；官方推荐；"This is the fastest and most scalable way to render Remotion videos." | [lambda](https://www.remotion.dev/docs/lambda) |
| Lambda 限制 | 视频需短于约 80 分钟 Full HD（受 15 分钟超时约束）；10GB 存储，输出约 5GB 上限，"limiting the maximum video length to around 2 hours of Full HD video"；默认每区 1000 并发；Lambda 无 AV1 编码 | 同上 |
| Cloud Run / Vercel Sandbox / 自建 Node 服务 | 单机渲染，无分布式："Vercel Sandbox, Cloud Run, and custom servers do not support distributed rendering" | [compare-ssr](https://www.remotion.dev/docs/compare-ssr) |
| 自建分布式 | 官方给了蓝图（每块帧数必须相同，最后一块除外，否则 AAC 音频出伪影；所有块选项一致；`h264` 要改 `h264-ts` 等），同时劝退："Building your own distributed renderer is challenging" | [distributed-rendering](https://www.remotion.dev/docs/distributed-rendering) |
| 浏览器端渲染 | `@remotion/web-renderer`（WebCodecs）；`<OffthreadVideo>` 不支持；官方说客户端渲染的遥测默认开启且无法关闭 | [video-tags](https://www.remotion.dev/docs/video-tags)、[license FAQ](https://www.remotion.dev/docs/license/faq) |

### 2.5 官方"人 + 工具"产品形态

模板页共 23 个模板（20 免费 + 3 付费），与我们相关的有：TikTok（逐词字幕）、Recorder（口播生产工具）、Prompt to Motion Graphics SaaS、Prompt to Video（图片 + 配音故事）、Audiogram / Music Visualization、Code Hike（代码讲解）、Overlay（给剪辑软件做叠加层）、付费的 Editor Starter 和 `<Timeline />`（[templates](https://www.remotion.dev/templates)，WebFetch 摘要）。

---

## 3. 给 agent 的设计

### 3.1 官方做了什么

| 手段 | 内容 | 出处 |
|---|---|---|
| **AI 友好文档** | 任意文档页加 `.md` 返回 Markdown；支持 `Accept: text/markdown` 内容协商；每页有 "Copy as Markdown"；`llms.txt` 索引 | [ai](https://www.remotion.dev/docs/ai/)、[llms.txt](https://www.remotion.dev/llms.txt) |
| **System prompt** | 公开的 LLM 系统提示 `system-prompt.txt`（约 11 KB），"teaches the general LLM Remotion's APIs and best practices"，并提醒 "Be careful of context rot" | [system-prompt.txt](https://www.remotion.dev/system-prompt.txt)、[generate](https://www.remotion.dev/docs/ai/generate) |
| **Agent Skills** | `remotion-dev/skills` 仓库，13 个 skill：`best-practices`（总入口）、`create`、`markup`、`studio`、`render`、`maps`、`captions`、`saas`、`interactivity`、`docs`、`license`、`upgrade`、`multimedia`；安装 `npx skills add remotion-dev/skills` / `npx remotion skills add` / `gh skill install remotion-dev/skills`；我读过的 skill（markup / create / render / captions / saas / docs / interactivity）frontmatter 都写着 `version: 4.0.539`，与框架版本绑定 | [skills](https://www.remotion.dev/docs/ai/skills)、[repo](https://github.com/remotion-dev/skills) |
| **各家插件** | Claude Code 插件 `remotion-dev/claude-code-plugin`（`claude plugin marketplace add remotion-dev/claude-code-plugin`，用法 `/remotion:remotion-create <prompt>`）；另有 Codex / Cursor / Copilot / Kimi Code 插件 | [claude-code-plugin](https://www.remotion.dev/docs/ai/claude-code-plugin)、[plugins](https://www.remotion.dev/docs/ai/plugins) |
| **MCP（已关停）** | 2026-10-26 关停，转向 `/remotion-docs` skill。原文理由见下 | [mcp](https://www.remotion.dev/docs/ai/mcp) |
| **SaaS 模板** | Prompt to Motion Graphics：LLM 生成 TSX 字符串，浏览器内用 `@babel/standalone` 转译 + `new Function` 注入 Remotion API 实时编译，Player 预览，Lambda 导出 | [ai-saas-template](https://www.remotion.dev/docs/ai/ai-saas-template)、[dynamic-compilation](https://www.remotion.dev/docs/ai/dynamic-compilation) |

MCP 关停的四条原文理由（[mcp](https://www.remotion.dev/docs/ai/mcp)）：
- "Its data could be less current than the Remotion documentation."
- "Remotion paid the token costs for MCP requests."
- "Installing MCP servers was difficult, and agents did not invoke them reliably."
- "It duplicated the `/remotion-docs` skill."

两种使用场景的官方界定："Use this template if you want to build a SaaS with Remotion and AI. Use Agent Skills if you want to prompt videos for yourself. Unlike the Agent Skills workflow, this template does not have access to an operating system or the filesystem."（[ai-saas-template](https://www.remotion.dev/docs/ai/ai-saas-template)）

SaaS 模板的防护清单（同上）："Input Validation - prevents end-user misuse"、"Sanitation - cleans nondeterministic LLM outputs"、"Self-correction - automatically retries on compilation errors"。生成文档还提到原始输出常带 markdown 围栏需要清洗，以及 import 语句要剥掉再注入 API；动态编译的安全提示是 "this code runs in the global scope of the browser"，生产环境建议放沙箱 iframe（[generate](https://www.remotion.dev/docs/ai/generate)、[dynamic-compilation](https://www.remotion.dev/docs/ai/dynamic-compilation)）。

### 3.2 工作流设计：让人始终在回路里

`remotion-create` skill 要求：

> "As soon as the project can run, open Remotion Studio in the browser before writing or changing the composition... Keep Studio running while you work so the user can see changes as they appear and steer the result."（[remotion-create SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-create/SKILL.md)）

`remotion-markup` 要求视觉自检："Open the Remotion Studio to check visual changes... You can also use Rendering to inspect one or several frames as images."，对应命令 `npx remotion render [composition-id] out/frames --frames=0,30,90 --image-format=png`（[remotion-render SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-render/SKILL.md)）。

人和 agent 同时改同一份代码是被明确考虑的：`remotion-markup` 的第一条规则是 "Preserve user changes. Users may make edits in the code outside of the conversation. If you detect a surprising change made in the meanwhile, don't overwrite it, assume it was intentional or ask for confirmation."（[markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md)）。这是 Studio 写回源码带来的"双写者"问题。

### 3.3 规则原文 → 暗示 agent 常犯的错

"直接证据"列只填我实际找到的出处；没有的写"无（推断）"。

| 规则原文（节选） | 出处 | 暗示的常见错误 | 直接证据 |
|---|---|---|---|
| "Drive animations using `useCurrentFrame()` and `interpolate()`. CSS `transition` or `animation` will not render correctly... Tailwind animation class will not render correctly" | [markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md) | 写 CSS keyframes / `transition` / Tailwind `animate-*` / `setTimeout`，预览看着对，多标签页渲染时闪烁或错位 | 第三方 skill 页面和博客都列为 AI 常见错误（[openreplay](https://blog.openreplay.com/making-videos-claude-code-remotion/)）；官方 [flickering](https://www.remotion.dev/docs/flickering) |
| "Remotion needs all of the React code to be deterministic. Therefore, it is forbidden to use the Math.random() API." | [system-prompt.txt](https://www.remotion.dev/system-prompt.txt) | 用 `Math.random()` / `Date.now()` | 无（推断） |
| "Keep `useCurrentFrame()` in the smallest component that needs it... otherwise unrelated descendants re-render on every frame." | [markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md) | 在根组件订阅帧，整棵树每帧重渲染 | 无（推断） |
| 秒转帧必须用 `useVideoConfig()` 的 `fps`，写成 `trimBefore={4 * fps}`；"Values are in frames." | [markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md)、[embedding-videos.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/embedding-videos.md) | 把秒当帧传（`trimBefore={2}` 只有 2 帧）；硬编码 fps；漏解构 `useVideoConfig()` 得到 `NaN` | **skills 仓库自己的文档就出过这两个错**：[skills#19](https://github.com/remotion-dev/skills/issues/19)（"Values are in seconds" 写错、`const fps = useVideoConfig()` 导致 `NaN`）、[skills#15](https://github.com/remotion-dev/skills/issues/15)；第三方也记录了 "Claude may assume a default [fps] that doesn't match the composition"（[openreplay](https://blog.openreplay.com/making-videos-claude-code-remotion/)） |
| 视频/音频用 `@remotion/media` 的 `<Video>` / `<Audio>`；`<Html5Video>` 与 `<OffthreadVideo>` "are not optimized" | [performance](https://www.remotion.dev/docs/performance)、[video-tags](https://www.remotion.dev/docs/video-tags) | 沿用训练数据里的旧写法：`<Video>` from `remotion`、`<OffthreadVideo>`、`startFrom` / `endAt`、`<Sequence>` | API 改名史有文档背书：`startFrom`/`endAt` → `trimBefore`/`trimAfter`（4.0.319），`trimAfter` 又被 `durationInFrames` 取代（4.0.530），`<Video>` 改名 `<Html5Video>`（[offthreadvideo](https://www.remotion.dev/docs/offthreadvideo)、[html5-video](https://www.remotion.dev/docs/html5-video)）。官方 `system-prompt.txt` 里仍写 `trimAfter`，与 skills 里的 `durationInFrames` 不一致 |
| 多个 Sequence 要写 `from`，否则都从第 0 帧开始 | 社区总结 | 所有动画同时触发 | 第三方："the cause is almost always missing `from` offsets on `<Sequence>` components"（[openreplay](https://blog.openreplay.com/making-videos-claude-code-remotion/)） |
| 用 `scale` / `translate` / `rotate` CSS 属性而不是 `transform` 字符串；`interpolate()` 必须内联在 `style` 里 | [markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md) | 把插值抽到变量里再拼 `transform: scale(...)`，Studio 无法写回 | 无（推断） |
| "keep `defaultProps` as an inline object literal on `<Composition>`... Do not store it in a variable, import it, spread it, create it with a helper, or wrap it in `satisfies`." | [compositions.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/compositions.md) | agent 习惯性抽常量、展开、封装，破坏 Studio 写回 | 无（推断） |
| "Write each independently editable instance as its own JSX node... Use loops for output intentionally controlled as one template" | [timeline-structure.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-interactivity/timeline-structure.md) | 用 `.map()` 生成所有片段，Studio 里每段不能独立编辑 | 无（推断） |
| "Put a shot's video, captions, and source-synchronized overlays under one timing boundary. Apply `trimBefore`, `durationInFrames`, and `playbackRate` to that boundary once." | [video-editing.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/video-editing.md) | 只给 `<Video>` 加 trim，字幕兄弟节点不继承，剪完字幕错位；改变换层级时偷偷改了 `useCurrentFrame()` 的坐标 | [display-captions.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-captions/display-captions.md) 明确提醒 "its sibling captions do not inherit those props" |
| "If cutaways cover a speaking video, retain the underlying speech or deliberately separate its audio; removing an obscured video can also remove the voice." | [video-editing.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/video-editing.md) | 为了"省事"删掉被遮挡的视频层，声音一起没了 | 无（推断） |
| "Transcribe each original source video or audio file individually. Keep the resulting caption timestamps relative to the beginning of that source file... Do not add composition offsets or flatten multiple transcripts into composition-wide timestamps." | [transcribe-captions.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-captions/transcribe-captions.md) | 把多段转写拍平成全片时间戳，一调剪辑点字幕全废 | 无（推断） |
| 用 FFmpeg 裁剪时 "You MUST re-encode the video to avoid frozen frames at the start of the video."；首选 `trimBefore` + `durationInFrames`（非破坏性） | [ffmpeg.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/ffmpeg.md) | `-ss ... -c copy` 无重编码裁剪，开头冻帧 | 无（推断） |
| 字体：Google Fonts 用 `@remotion/google-fonts`（"automatically blocks rendering until the font is ready"）；"Only call measurement functions after fonts are loaded." | [google-fonts.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/google-fonts.md)、[measuring-text.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/measuring-text.md) | 字体没加载完就截图或量文字，布局错 | 见 §4 字体条目 |
| 安装依赖用 `npx remotion add <pkg>`（保证版本一致） | [markup SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md) | 直接 `npm i @remotion/xxx`，各包版本不一致 | 官方有 [version-mismatch](https://www.remotion.dev/docs/version-mismatch) 专页（未读全文，仅据页面存在推断） |
| "You are designing a video, not a webpage."；1080 宽时主标题至少 84px、辅助文字至少 44px，安全区左右 80px、上下 100px | [video-layout.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-create/video-layout.md) | 用网页尺度排版，字太小、贴边 | 无（推断） |
| 动态编译生成的代码"AI-generated code typically includes import statements"，需剥离；原始输出常带 markdown 围栏 | [dynamic-compilation](https://www.remotion.dev/docs/ai/dynamic-compilation)、[generate](https://www.remotion.dev/docs/ai/generate) | 输出格式不稳，不能直接 eval | 官方文档本身 |
| Lambda 部署时 "never ask the user to paste secrets into chat" | [saas rendering.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-saas/rendering.md) | 让用户把 AWS key 贴进对话 | 无（推断） |

### 3.4 对 skill 本身质量的观察

skills 仓库的 open issue 能看到 skill 自身的维护问题：安装后 hub 里六个子 skill 链接全部失效 [skills#24](https://github.com/remotion-dev/skills/issues/24)；`spring()` 指引前后不一致且有示例错误 [skills#20](https://github.com/remotion-dev/skills/issues/20)；以及上面两个单位错误（#19、#15）。这说明"给 agent 的规则文档"本身也需要测试，而不是写完就算。

---

## 4. 痕迹（GitHub issue / 文档 / 第三方）

每条一行。Discord 内容没有检索到，不在其中。

### 4.1 渲染速度（长视频 / 4K / 并发）

- 66,650 帧（1080p60，约 18.5 分钟）渲染随进度变慢，原因是 `renderFrameWithOptionToReject` 每帧重建全部历史资产（O(n²)）：4.0.512 总耗时 50.8 分钟（平均 21.9 fps，从 25 fps 一路降到 14.5 fps），修复后 28.7 分钟（38.7 fps）；环境 Linux/WSL2、16 核、`concurrency: 6`，含旁白、背景音乐和 `<OffthreadVideo>` 片段。已关闭。[#11911](https://github.com/remotion-dev/remotion/issues/11911)
- 48 核 / 350GB 的 VPS 上，并发调高反而更慢，CPU 大量空闲；维护者回应："All the `concurrency` tab does it set the number of Chrome tabs"，并怀疑 "Extracting the frames from a video is a process that cannot be well parallelized." [#4300](https://github.com/remotion-dev/remotion/issues/4300)
- 维护者整理的单机性能 issue："Videos are extracted frame-by-frame, making it difficult to parallelize. Composition with heavy OffthreadVideos can see significant slowdowns at high concurrency." 提议的方向包括多浏览器实例、预处理、乐观缓存。[#4664](https://github.com/remotion-dev/remotion/issues/4664)
- 官方性能页的建议：用 `@remotion/media` 的 `<Video>`；用 `benchmark` 找并发；GPU 类效果（`box-shadow`、`filter: blur()`、渐变背景、WebGL）在无 GPU 的云机上会成为瓶颈；PNG 比 JPEG 慢，VP8/VP9 编码很慢；降分辨率用 `--scale`。[performance](https://www.remotion.dev/docs/performance)
- Lambda 渲染"1 小时以上视频卡在随机分块"的用户报告（Remotion 4.0.150，同一输入有时成功有时失败），据说后续版本改善。我只通过搜索摘要看到，URL 是第三方镜像，未能在 github.com 核实：[git.tdem.in 镜像 #3883](https://git.tdem.in/remotion-dev/remotion/issues/3883)。**[第三方 / 未核实]**
- 第三方估算："A 30-second composition at 30fps means 900 browser screenshots"，长或重的合成在笔记本上可能耗时很久，文中没有实测时间。[openreplay](https://blog.openreplay.com/making-videos-claude-code-remotion/) **[第三方]**
- 官方 Lambda 加速博客有 1 / 10 / 40 分钟输出的图表，数字我无法可靠配对，不采用。[faster-lambda](https://www.remotion.dev/blog/faster-lambda)

### 4.2 内存与进程

- `<OffthreadVideo>` 在 Cloud Run 上 "Memory limit exceeded"。[#2611](https://github.com/remotion-dev/remotion/issues/2611)
- `<OffthreadVideo>` 帧缓存默认是渲染开始时可用内存的一半（`--offthreadvideo-cache-size-in-bytes`）；`@remotion/media` 另有 `--media-cache-size-in-bytes`，同样默认一半。[offthreadvideo-cache-size-in-bytes](https://www.remotion.dev/docs/options/offthreadvideo-cache-size-in-bytes)（搜索摘要）
- 父进程死亡后 `chrome-headless-shell` 孤儿进程不回收（PPID=1）。[#7207](https://github.com/remotion-dev/remotion/issues/7207)
- `@remotion/renderer` 上报内存的口径问题（issue 标题："Report available memory instead of free pages"，已关闭；我只读了标题，未读正文）。[#10654](https://github.com/remotion-dev/remotion/issues/10654)

### 4.3 macOS / 内置 Chrome 稳定性（与本机直接相关）

- macOS 27.0.1 / M4 Pro、Remotion 4.0.537：部分 composition 渲染到一半 `Page crashed!`（默认并发 6 时在 1,325 / 1,500 帧处崩溃），原因是内置的 mac-arm64 Chrome Headless Shell 编进了 V8 DCHECK；改用 `--browser-executable` 指向 Google 的 chrome-headless-shell 157.0.8080.0 后同一批渲染全部完成（含 8,856 帧 / 295 秒 1080p 的整条合成）。open。[#12680](https://github.com/remotion-dev/remotion/issues/12680)
- 同一问题的"关闭 DCHECK"PR 被作者自己关闭，理由是这是 Chrome 157 里真实的 V8 bug（V8 `regress-567574900`，Chromium 158 修复），计划改为升级 Chromium 158 构建（#12638）。[#12681](https://github.com/remotion-dev/remotion/pull/12681)
- macOS 26.6.2 / arm64、Remotion 4.0.539：内置 headless shell 下每次渲染都报 `Waiting for root component to load` 超时；Google 官方同版本 headless shell 和 `--chrome-mode=chrome-for-testing` 正常；维护者在 macOS 15.7.9 上未复现，仍在排查。open。[#12633](https://github.com/remotion-dev/remotion/issues/12633)
- Chrome 157 在 `HtmlInCanvas` 下捕获崩溃。open。[#12369](https://github.com/remotion-dev/remotion/issues/12369)

### 4.4 音画同步

- 渲染出的 MP4 音频整体晚 2048 个采样（48 kHz 下 42.67 ms）：libfdk_aac 的编码延迟在经过 ADTS 中间格式时丢失；本地和 Lambda 都有。已关闭（2026-09-26）。[#11704](https://github.com/remotion-dev/remotion/issues/11704)
- Player 中混合帧率素材时 `<Video>` 标记与画面失步，约回跳 4 帧（4.0.374 → 4.0.459 回归）。已关闭。[#7300](https://github.com/remotion-dev/remotion/issues/7300)
- 播放中改 `durationInFrames` 导致音频卡顿。已关闭。[#12279](https://github.com/remotion-dev/remotion/issues/12279)
- `getMediaSyncAction` 把媒体秒数和 `performance.now()` 时间戳直接比较。open（2026-10-24）。[#12588](https://github.com/remotion-dev/remotion/issues/12588)
- 变速时 `<Video>` / `<Audio>`（`@remotion/media`）默认音调随之改变，需 `preservePitch` 才保持；旧组件默认保持音调。[video-tags](https://www.remotion.dev/docs/video-tags)

### 4.5 素材与解码

- `<OffthreadVideo>` 的 HEVC 帧在 look-ahead 计数下溢后重复。已关闭。[#11566](https://github.com/remotion-dev/remotion/issues/11566)
- 浏览器端渲染 + `<Video>` 在 Windows 上卡在 0.2 秒（`delayRender` 58 秒超时，H.264 手机素材），要求暴露解码器 `hardwareAcceleration`。open。[#11393](https://github.com/remotion-dev/remotion/issues/11393)
- 素材不可 seek 的三类原因：缺 `Content-Range`、缺 `Content-Length`、MP4 没有 faststart。解法：用 `@remotion/media` 的 `<Video>`、换支持 Range 的主机、下载到本地用 `staticFile`。[non-seekable-media](https://www.remotion.dev/docs/non-seekable-media)
- 老版本 `<OffthreadVideo>` 遇到 H.264 时间戳损坏或 VP8 + PNG 会走"整段读取"的慢路径；官方说 4.0 起该警告不再出现。[slow-method-to-extract-frame](https://www.remotion.dev/docs/slow-method-to-extract-frame)

### 4.6 字体与超时

- 本地字体加载总是超时。[#5843](https://github.com/remotion-dev/remotion/issues/5843)；加载 Noto Sans JP 超时。[#5037](https://github.com/remotion-dev/remotion/issues/5037)
- 同一项目里出现两份打包的 `remotion` 时，`delayRender` 计时器被孤立，"continueRender silently no-ops; long renders die at timeout"。已关闭。[#10856](https://github.com/remotion-dev/remotion/issues/10856)
- 默认 `delayRender` 28 秒超时；常见原因是没调 `continueRender()`、云端 VPC 阻断外网素材。[timeout](https://www.remotion.dev/docs/timeout)
- 闪烁的另两个来源：渲染器不等资源；避免用 `background-image` 和 `mask-image`。[flickering](https://www.remotion.dev/docs/flickering)
- `--concurrency=1` 能缓解部分闪烁，但"it is way slower... It will also block rendering via Remotion Lambda"。[flickering](https://www.remotion.dev/docs/flickering)

### 4.7 AI 生成代码的常见 bug

- 单位错误与 `NaN`：见 §3.3 的 [skills#19](https://github.com/remotion-dev/skills/issues/19)、[skills#15](https://github.com/remotion-dev/skills/issues/15)。
- CSS 动画 / `setTimeout` 代替 `interpolate`；原生 `<img>` `<video>` `<audio>` 代替 Remotion 组件；硬编码时长而不用 `calculateMetadata`；Sequence 漏 `from`；`durationInFrames` 与组件内帧数不一致；没说明 fps 时假设错误。**[第三方]** [llmbase 上的第三方 remotion skill 页](https://llmbase.ai/skills/oakoss/remotion/)、[openreplay](https://blog.openreplay.com/making-videos-claude-code-remotion/)
- 安装命令随版本变化，教程里的命令常失效："The skills packaging has been moving, so don't trust a copied command." **[第三方]** [openreplay](https://blog.openreplay.com/making-videos-claude-code-remotion/)
- 官方 MCP 被 agent 调用不稳定，最终下线（§3.1）。

---

## 5. 擅长与不擅长

### 5.1 有证据的"擅长"

| 类型 | 证据 |
|---|---|
| **字幕动画 / 逐词高亮** | 有 `Caption` 标准类型、`@remotion/whisper-webgpu` 本地转写（GPU，Linux arm64 不可用）、Basic Captions 元素、TikTok 逐词字幕模板（[captions SKILL](https://github.com/remotion-dev/skills/blob/main/skills/remotion-captions/SKILL.md)、[transcribe-captions.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-captions/transcribe-captions.md)、[templates](https://www.remotion.dev/templates)） |
| **知识 / 讲解 / 数据可视化 / 地图** | 浏览器的排版和矢量能力（"The web is the world's most popular graphics engine"，[why](https://www.remotion.dev/docs/why)）；maps skill（Mapbox / MapLibre / MapTiler / CesiumJS 3D 飞越）；Code Hike、Audiogram、图表类示例 |
| **模板化批量生成** | `inputProps` + `calculateMetadata` + Lambda；官方 FAQ 认可的用例："Allowing users to create and render their own personalized video based on your Remotion template."（[license FAQ](https://www.remotion.dev/docs/license/faq)）。"可渲染百万条"是官方营销表述，我没有独立验证 |
| **AI 配音 + 配图的"故事视频"** | Prompt to Video 模板；`voiceover.md` skill 接 ElevenLabs TTS（[voiceover.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/voiceover.md)） |
| **转场 / 叠加 / 特效** | `@remotion/transitions`、约 76 种内置 `effects`、Overlay 模板 |
| **可编程的口播生产** | Recorder：摄像头 + 屏幕独立录制（最多 4 路）、Whisper.cpp 自动字幕（首次需装 1.5GB 模型）、按字幕首尾词自动裁头尾（默认开头留 0.25s、结尾留 0.5s）、场景拼接、转场（[recorder](https://www.remotion.dev/docs/recorder/captions)、[silence-removal](https://www.remotion.dev/docs/recorder/editing/silence-removal)） |

### 5.2 有证据的"不擅长 / 要小心"

| 场景 | 证据 | 说明 |
|---|---|---|
| **对长素材做大量精确剪切** | 帧抽取难并行（[#4664](https://github.com/remotion-dev/remotion/issues/4664)）；48 核不提速（[#4300](https://github.com/remotion-dev/remotion/issues/4300)）；66,650 帧 21.9→38.7 fps（[#11911](https://github.com/remotion-dev/remotion/issues/11911)） | **[推算]** 以这一个数据点外推：1 小时 @30fps = 108,000 帧，对应约 47–82 分钟渲染（16 核、含旁白+配乐+若干 OffthreadVideo 片段的合成；单点样本，不能当基准）。相比之下，直接流式 FFmpeg 切割不需要逐帧截图（该点是我基于工作原理的判断，未找到 Remotion 与 FFmpeg 的直接对比） |
| **口播粗剪（长录音）** | Recorder 文档："The recommended way is to record many small bite-sized clips... If this is unfeasible and you already have a long recording that you would like to split after the fact, you can use the following script" 后面是一段 bun 脚本，用 `bunx ffmpeg` 在一个切点把各路文件一分为二，并明确标注 "The following is a destructive action - commit your changes before you run this."（[cutting-clips](https://www.remotion.dev/docs/recorder/editing/cutting-clips)） | Remotion 自己的口播工具把"切长录音"留给外部 ffmpeg；其静音裁剪只处理首尾（以字幕首尾词为准），不是句中静音（[silence-removal](https://www.remotion.dev/docs/recorder/editing/silence-removal)；skill 版的静音检测也只讲 leading / trailing，[silence-detection.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/silence-detection.md)） |
| **云端长视频** | Lambda 上限：约 80 分钟 Full HD、输出 ~5GB / ~2 小时、无 AV1（[lambda](https://www.remotion.dev/docs/lambda)） | 超出要走单机 SSR，失去分布式 |
| **游戏实况 / 直播切片素材** | 快速路径 `<Video>` 只支持 H.264 / VP8 / VP9 与 AAC / MP3 / Opus 等；H.265、AV1、AC3 要回退 `<OffthreadVideo>`（速度从 "Fastest" 降到 "Fast"，且不能 loop、不能部分下载）（[video-tags](https://www.remotion.dev/docs/video-tags)） | OBS / NVENC 常见的 HEVC / AV1 会落到慢路径；直播流本身（实时输入）未找到支持证据 |
| **实时性** | 渲染是离线批处理；预览靠 Studio / Player，并有专门的 "Playback issues" 文档（[playback-issues](https://www.remotion.dev/docs/player/playback-issues)）；变调只在成片里生效（§1.3） | 预览与成片可能不一致；未找到实时输出 / 推流能力的证据 |
| **鬼畜（倒放、剧烈变速、变调）** | "Reverse playback is not supported."；`playbackRate` 是单值；`toneFrequency` 只在 SSR 生效；变速默认改变音调（[embedding-videos.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/embedding-videos.md)、[video-tags](https://www.remotion.dev/docs/video-tags)） | 倒放需预先用 ffmpeg 生成倒放素材；未找到逐帧变速曲线的原生支持证据 |
| **混剪卡点（节拍对齐）** | 文档与 skills 里有音频可视化（`visualizeAudio`、FFT 条形/波形），没有节拍 / onset 检测（我在已取得的文档与 skills 文本中检索 beat / bpm / onset 均无命中） | 节拍点需外部算出，再作为 JSON 喂给 composition。**未找到证据** 显示 Remotion 内置节拍检测 |
| **macOS 渲染稳定性（当下）** | 见 §4.3 | 内置 Chrome 在最新 macOS 上有 open 问题 |
| **字体 / CJK** | Noto Sans JP 超时（[#5037](https://github.com/remotion-dev/remotion/issues/5037)）、本地字体超时（[#5843](https://github.com/remotion-dev/remotion/issues/5843)） | 都已关闭，但说明首次加载需要处理 |

### 5.3 官方定位

- 自述："Remotion aims to at least perform similarly than traditional video editing programs."，同页前一句是 "Video rendering is one of the most heavy workloads a computer can take on."（[performance](https://www.remotion.dev/docs/performance)）。
- 官方推荐的用法顺序是"视频即代码，可组合、可参数化、可嵌入应用、可渲染到云"（[why](https://www.remotion.dev/docs/why)），不是"视频剪辑软件的替代品"。

---

## 6. 许可证

原文：[LICENSE.md](https://github.com/remotion-dev/remotion/blob/main/LICENSE.md)、[License FAQ](https://www.remotion.dev/docs/license/faq)、[定价](https://www.remotion.pro/license)。

### 6.1 条款要点

| 项目 | 内容 |
|---|---|
| **免费资格（Free License）** | 个人；**最多 3 名员工的盈利组织**；非营利 / 非盈利组织；"evaluating whether Remotion is a good fit, and are not yet using it in a commercial way"。免费版**允许商业使用**，功能与付费版无差别 |
| **付费资格** | 不在上述范围内的公司需要 Company License。判定依据是公司**总人数**，"a 20-person company with one Remotion user" 仍需要（FAQ）。单人注册公司只要总人数 ≤ 3 仍可免费 |
| **Remotion for Creators** | 25 美元 / 月 / 人（seat）。FAQ："per person who writes Remotion code themselves or using agentic coding tools" |
| **Remotion for Automators** | 每次渲染 0.01 美元，**最低 100 美元 / 月**。适用于程序化调用渲染的组织 |
| **什么算 automation** | 自有代码程序化调用 `renderMedia()` / `renderStill()` / `renderFrames()` / `renderMediaOnLambda()` / Cloud Run / Vercel / Web 渲染 API，或 `npx remotion render` / `still` / `lambda render` 等 CLI，或使用 `<Player>`。FAQ："organizations building video editors, prompt-to-video tools, automated video pipelines or using the Remotion Player will fall under Remotion for Automators." 预览不算渲染 |
| **Enterprise** | 起价 500 美元 / 月，含定制条款、私有支持、每月咨询、Editor Starter |
| **禁止** | "It is not allowed to copy or modify Remotion code for the purpose of selling, renting, licensing, relicensing, or sublicensing your own derivate of Remotion."；不得让用户上传自己的 Remotion 项目到你的服务器渲染 |
| **允许** | 任何商业用途，只要不是卖 Remotion 本身、也不是让他人绕开购买许可。例："Allowing users to create and render their own personalized video based on your Remotion template." 允许做"用 AI 生成 Remotion 代码并渲染"的服务，客户无需许可证；用户可编辑 AI 生成的代码，但不能自带代码上传 |
| **性质** | "source-available software, but it is not open source software"（FAQ） |
| **不含编解码专利授权** | "A Remotion license covers your use of Remotion only. It does not include a license to third-party patents, such as... H.264/AVC, HEVC or AAC."（FAQ） |
| **遥测** | 浏览器端渲染遥测默认开启且无法关闭；服务端目前无自动遥测，5.0 起对 Automators 强制 |

### 6.2 Remotion 5.0 的变化（尚未合并）

PR [#3750](https://github.com/remotion-dev/remotion/pull/3750)（open）：新许可证"will ship with Remotion 5.0 and apply for anyone using Remotion 5.0 or later"，有两处实质变化：承包商也计入团队人数（此前"a company could only work with contractors and never have to get a company license"）；公司许可证绑定新写的 terms and conditions。

### 6.3 对 autocut 把 Remotion 当可选后端意味着什么

下面区分"原文"与"我的推断"。**FAQ 没有直接回答"依赖 Remotion 的开源工具"这个问题**（FAQ 自述 "doesn't directly address open-source tools"），所以以下带"推断"的条目需要你们自己判断或直接问 hi@remotion.dev。

- 你自己（个人）使用：免费，包括商业用途。（原文）
- autocut 作为开源项目，Remotion 只是用户自行 `npm i` 的**可选依赖**：许可义务落在**实际运行 Remotion 的人或公司**，按他们的总人数判断。（推断）
- **不要把 Remotion 源码 vendoring / fork 进 autocut 仓库再分发**：落入"derivative"禁令的风险。（推断；原文只禁止"为出售、出租、授权自己的衍生版本"）
- 若某个 ≥4 人的公司用 autocut 的 Remotion 后端：该公司要买许可。agent（如 Claude Code）写 TSX 的人算 Creators seat（25 美元 / 月 / 人）；而 autocut 自动调用 `npx remotion render` 的批处理，按定义属于 automation，需要 Automators（0.01 美元 / 渲染，最低 100 美元 / 月）。两者可同时开通，合计最低 100 美元 / 月。（原文 + 推断）
- 自动化批渲染 + 我们不让用户传 Remotion 代码，只让用户传 autocut JSON、由我们固定的 composition 渲染：更接近官方认可的 "personalized video based on your Remotion template"，而不是被禁止的 "submit any Remotion video"。（推断）
- 在 Remotion 渲染里**输出 H.264 / HEVC / AAC** 的编解码专利费用不含在 Remotion 许可内。（原文）
- 使用 5.0+ 后，承包商计入人数，Automators 需开遥测。（原文，PR 未合并）
- 替代方案（如 FFmpeg 滤镜、MLT、其他合成引擎）的许可证不在本报告范围。

---

## 7. 本机可用性（只检查，未下载、未安装）

### 7.1 本机环境

| 项 | 值 |
|---|---|
| 系统 | macOS 27.0.1，arm64（Remotion 要求 macOS 15+，[getting started](https://www.remotion.dev/docs#system-requirements)） |
| Node / pnpm / ffmpeg | v26.11.1 / 10.10.0 / 9.0.2 |
| Chrome | `/Applications/Google Chrome.app` = 156.0.8078.48（完整 Chrome，非 headless shell） |
| 其他 headless shell | `~/.cache/puppeteer/chrome-headless-shell/` 里有 `mac_arm-148.0.7778.97`（191 MB）和 `mac_arm-146.0.7680.153`（189 MB），`--version` 均显示 "Google Chrome for Testing" |
| 其他发现 | `~/Downloads/remotion-main.zip`（611 MB，2026-10-24），是仓库快照，不影响 npm 安装 |

### 7.2 缓存命中

只读检查：pnpm store `/Users/lishuyu/Library/pnpm/store/v10`（7.9G）的 `index/*/` 文件名；npm cache `~/.npm/_cacache`（4.6G）的 `index-v5` 键。

| 包 | pnpm store | npm cache |
|---|---|---|
| `remotion` | 无 | 无 |
| `@remotion/cli` | 无 | 无 |
| `@remotion/renderer` | 无 | 无 |
| `@remotion/player` | 无 | 无 |
| `@remotion/*` 其余 | 无（全 store 内无任何含 "remotion" 的条目） | 无 |
| `react` | 有：17.0.2、18.3.1、19.1.0、19.2.1、19.2.4、19.2.5、**19.2.8**、19.3.0 | 有：18.3.1、19.2.8、19.3.0、19.3.1 |
| `react-dom` | 有：17.0.2、18.3.1、19.1.0、19.2.1、19.2.4、**19.2.8**、19.3.0 | 有：18.3.1、19.2.8、19.3.0、19.3.1 |
| 其他可复用的 | `esbuild` 多版本（含 darwin-arm64 的 0.28.1）、`zod` 多版本（含 4.x）、`ws`、`undici-types` 等 | 未逐个查 |

Remotion 4.0.539 对 react / react-dom 的要求是 `>=16.8.0`（[npm registry 元数据](https://registry.npmjs.org/remotion/latest)）。

### 7.3 首次安装要下载多少

**npm 包部分（实测方法）**：在 scratchpad 的隔离目录里，对 `remotion`、`@remotion/cli`、`@remotion/renderer`、`@remotion/player`、`@remotion/media`、`react`、`react-dom` 执行 `npm install --package-lock-only --ignore-scripts`（只解析依赖、只拉包元数据，不下载 tarball、不安装、不跑脚本），得到 299 个依赖项，按 `os: darwin` / `cpu: arm64` 过滤后 **259 个**；再对每个 tarball URL 发 1 字节 Range 请求读取总大小。

| 项 | 大小 |
|---|---|
| 259 个 tarball 总计 | **58.3 MB** |
| 其中 pnpm store 已有同名同版本的 | 13.6 MB |
| 需要新下载（扣除 store 命中） | **约 44.7 MB** |
| 最大几项 | `@rspack/binding-darwin-arm64` 16.7 MB；`@remotion/compositor-darwin-arm64` 8.0 MB（自带 FFmpeg / ffprobe 与原生二进制）；`@esbuild/darwin-arm64` 4.4 MB（store 已有）；`webpack` 3.2 MB + 1.1 MB（依赖树里有两个版本）；`@remotion/studio` 2.5 MB；`mediabunny` 2.2 MB |

注意：pnpm 实际装的版本可能与此次 npm 解析不同；`create-video` 的 blank 模板还会带 TypeScript / ESLint / Prettier 等 devDependencies，我没有测，所以这是**下限偏乐观的估计**。

**Chrome Headless Shell 部分（HEAD 请求，未下载）**：

| 来源 | 大小 |
|---|---|
| Remotion 自带构建 `https://remotion.media/chromium-headless-shell-mac-arm64-157.0.8080.0.zip` | **179,230,663 字节（约 179 MB）** |
| Google 官方 chrome-headless-shell 157.0.8080.0（mac-arm64，`chrome-for-testing-public`） | 103,864,219 字节（约 104 MB） |

下载逻辑见 [get-chrome-download-url.ts](https://github.com/remotion-dev/remotion/blob/main/packages/renderer/src/browser/get-chrome-download-url.ts)；默认存放在 `node_modules/.remotion/chrome-headless-shell/`（[chrome-headless-shell](https://www.remotion.dev/docs/miscellaneous/chrome-headless-shell)）。解压后的体积我没有测（本机 puppeteer 的同类 headless shell 解压后约 190 MB，仅作参考）。

**合计**：约 **224 MB**（44.7 + 179）；若 store 无任何命中则约 **237 MB**（58.3 + 179）。如果走"用 Google 官方 headless shell"路线（见下），下载量约 149 MB 或更少。

**我在测算过程中的网络开销**：依赖解析下载了包元数据（落在隔离缓存里，磁盘上 173 MB 未压缩 JSON，网络传输量未测但应小于此值），259 次 1 字节 Range 请求，2 次 HEAD 请求；没有下载任何 tarball 或 zip。探测目录在 `/private/tmp/claude-501/-Users-lishuyu-Codes-autocut/457cba72-f891-417a-bcd7-e9e176d74173/scratchpad/depprobe/`，可直接删除。

### 7.4 能否用本机 Chrome 代替

**配置上可以**：`--browser-executable <path>` / `Config.setBrowserExecutable()` / `renderMedia({browserExecutable})`（[browser-executable](https://www.remotion.dev/docs/options/browser-executable)）。**但官方明确不推荐**：

> "Remotion recommends using the Chrome Headless Shell version that is installed and pinned by Remotion. When using a full Chrome binary through `browserExecutable`, rendering behavior may differ between Chrome versions and may be less deterministic than Chrome Headless Shell."（[chrome-headless-shell](https://www.remotion.dev/docs/miscellaneous/chrome-headless-shell)）

具体到本机：

| 候选 | 评估 |
|---|---|
| `/Applications/Google Chrome.app`（156.0.8078.48，完整 Chrome） | 不推荐。文档说桌面 Chrome 已移除旧 headless 模式、Remotion 为此改用 headless shell，我**没有找到**"当前 Remotion 能稳定使用桌面 Chrome 渲染"的证据；版本也与 Remotion 固定的 157.0.8080.0 不同；用非默认浏览器时共享内存捕获等优化不生效（[shared-memory-capture](https://www.remotion.dev/docs/shared-memory-capture)："These requirements assume you use Remotion's default browser"） |
| `~/.cache/puppeteer/chrome-headless-shell/mac_arm-148.0.7778.97`（Google Chrome for Testing 的 headless shell） | **最值得试**：与 Remotion 自带的同类（headless shell），零下载；但版本 148 与 Remotion 测试的 157 不同，**未验证**。已有间接证据说明"Google 官方 headless shell + `--browser-executable`"在 macOS 27.0.1 / M4 Pro 上能跑通（#12680 用的是 157.0.8080.0 版本） |
| `--chrome-mode=chrome-for-testing` | 官方支持的备选；但它会另下载 Chrome for Testing，且文档说它用于 GPU 场景，"requires more dependencies and resources" |

**结合 §4.3**：本机恰好是 #12680 的环境（macOS 27.0.1 / Apple Silicon），Remotion 内置 headless shell 有 open 的崩溃问题，Remotion 团队计划的修复是 Chromium 158 构建（#12638）。所以"先下 179 MB 的内置 shell 再去踩坑"不是最优路径；如果要试，建议直接用上面第二行的本地 headless shell，或下载 Google 官方 157 版（约 104 MB）。

### 7.5 FFmpeg

Remotion 自带 FFmpeg / ffprobe（随 `@remotion/compositor-darwin-arm64`），"`ffmpeg` and `ffprobe` do not need to be installed... available via `npx remotion ffmpeg`"（[ffmpeg.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/ffmpeg.md)）；`setFfmpegExecutable()` 在配置文档中已被删除线标注（[config](https://www.remotion.dev/docs/config)）。因此渲染器用的是它自带的 FFmpeg，不是本机 9.0.2；但 autocut 自己的预处理 / 后处理仍可用本机 ffmpeg。

### 7.6 其他

- Node 26：**未找到官方支持声明**（文档用 `<MinNodeVersion />` 组件，我没取到具体值；issue 里的用户用 Node 24）。
- Remotion 支持 pnpm（skills 里给出 `pnpm exec remotion add`）。

---

## 8. 对 autocut 的启示

给出依据，不下结论。

### 8.1 三栏总表

| 可借鉴 | 可直接集成 | 不适合我们 |
|---|---|---|
| **1. "Skill + Markdown 文档"的 agent 接口**。官方放弃 MCP、押注 skill 的四条理由（数据过期、成本、难装、不可靠调用）值得我们对照自己的接口选型。`.md` 后缀、`Accept: text/markdown`、`llms.txt` 都是低成本做法。（§3.1） | **1. 合成型内容的渲染后端**：知识视频、字幕动画、数据可视化这类"画出来"而不是"切出来"的内容。`autocut JSON → 固定 composition → renderMedia`。证据：模板化参数渲染是它最成熟的用法（§1.4、§5.1） | **1. 长素材精确剪切本身**：口播粗剪、直播切片、游戏实况的切割。逐帧截图 + 帧提取难并行，Remotion 自己的 Recorder 也把长录音切分外包给 ffmpeg。（§5.2） |
| **2. 把规则按"错误模式"组织**，且给每条规则配可验证的反例。skills 的写法（"CSS transition will not render correctly"）就是针对 agent 常犯的错写的。同时要看到它自己的 skill 文档也出错（skills#19 / #15 / #24），说明规则文档要有测试。（§3.3、§3.4） | **2. 把 `@remotion/captions` 的 `Caption` JSON 当字幕交换格式**（MIT 许可，30 KB，无依赖）：`{text, startMs, endMs, timestampMs, confidence}`；转写时间戳相对源文件、不要拍平，这条规则对我们的"剪辑点会变"场景也成立。（§3.3、[npm 元数据](https://registry.npmjs.org/@remotion/captions/latest)） | **2. 把 TSX 当唯一时间线表示**：它不能确定性地导出到 OTIO（官方原话），代码里的时间线无法独立校验或 diff，对"人和 agent 共同编辑 + 可审阅"是负担。（§1.4） |
| **3. 回路里的人 + 视觉自检**：先开预览再改；用 `--frames=0,30,90 --image-format=png` 抽几帧给 agent 自己看。（§3.2） | **3. 用 Remotion Player 嵌入网页做审阅界面**（见 8.2 的前提）：能直接吃 JSON 驱动的 composition，`inputProps` 实时改。 | **3. 当前 macOS 上默认直接上内置 Chrome**：open 崩溃/超时问题（§4.3）。 |
| **4. 单位与时钟的明确约定**："everything is in frames, convert from fps"、"一个时间边界里放同一镜头的视频+字幕+叠加层"、"字幕时间戳相对源文件"。这些是剪辑工具里经典的错误来源，我们的时间线 JSON 可以在 schema 层面强制。（§3.3） | **4. 用 ffmpeg 做预处理、Remotion 做合成包装** 的分工，有官方先例（Recorder 的 `cut.ts`、silence-detection skill）。 | **4. 对鬼畜 / 倒放 / 变调预览**：不支持倒放，变调预览与成片不一致。（§5.2） |
| **5. 参数化三层**：`defaultProps`（可设计）→ `inputProps`（可覆盖）→ `calculateMetadata`（可派生时长）。对应我们的"模板 + 时间线数据 + 派生信息"。（§1.2） | **5. 动态元数据**：时长由素材决定（`getVideoDuration`）而不是手写。 | **5. 节拍对齐**：未找到内置能力，需外部算。（§5.2） |
| **6. 约束代码形状以换取可视化编辑**（inline 字面量、独立 JSX 节点）的取舍，它的代价是 agent 规则大量增加。如果我们选 JSON，等于把这些约束变成了 schema，规则更少。（§3.3） | | **6. 作为必需依赖**：许可（≥4 人公司付费，automation 另算）、安装体积（约 224 MB）、API 变化快（10 天 4 个版本，且有多次改名）。适合"可选后端"，不适合"核心"。（§6、§7、§3.3） |
| **7. 对许可敏感的分发方式**：可选依赖、不 vendoring 源码、用户自己 `npm i`。（§6.3，推断） | | |

### 8.2 四个具体问题的依据

**① 时间线用声明式 JSON 还是代码？**

| | 声明式 JSON | 代码（TSX，Remotion 式） |
|---|---|---|
| 证据 | Remotion 自己的 props 层、字幕、Recorder 的 `scenes` 都是 JSON，且为"Studio 可编辑"约束了代码形状（§1.4、§3.3）；`calculateMetadata` 从 JSON 派生时长 | 官方 Studio 能把画布拖拽写回 JSX 源码；表达力无上限（任意 React / CSS / WebGL）；agent 对 React 熟悉 |
| 优点 | 可校验（zod）、可 diff、可导出到别的后端（FFmpeg / NLE）、规则更少 | 一次写完就能渲染；能表达模板里没预留的效果 |
| 代价 | 表达力受 schema 限制，新效果要扩 schema | 无法确定性导出（OTIO 官方原话）；API 快速改名使训练数据过时（§3.3）；确定性/并行约束（§1.4）；agent 需要大量规则；LLM 生成的代码要沙箱（§3.1） |

**② 能否把 Remotion Player 当人工审阅界面？**

- 支持：能嵌进任意 React 应用并在运行时改 `inputProps`（[player](https://www.remotion.dev/docs/player)）；官方模板 Next.js / React Router 就是 Player + 云渲染。
- 要注意：Player 在多段素材场景有"黑帧、音频卡顿、跳动"的专门排查页；变调不可预览；混合帧率素材曾有失步回归（#7300）；`<Video>` 需要 CORS；**Player 在许可上算 automation**（公司用户要付 Automators）；它只能预览 Remotion composition，**不能预览 FFmpeg 路径出的结果**——如果 autocut 的主渲染器是 ffmpeg，审阅界面和成片可能不是同一个渲染器。
- 这一条依据偏"能做"，"值不值得"取决于主渲染器选哪个。

**③ 值不值得做"autocut 时间线 JSON → Remotion composition"后端，只给合成型内容用？**

- 有利证据：模板化参数渲染是官方认可且最成熟的用法；字幕、转场、效果、图表、地图都有现成组件；Lambda 可水平扩展；许可上"用户传 JSON、我们固定 composition"更接近允许的用例（推断）。
- 不利证据：安装体积约 224 MB；macOS 上内置 Chrome 当前有 open 问题；Remotion 5.0 许可会变（承包商计入、Automators 强制遥测）；渲染速度比流式 ffmpeg 慢（§5.2，推算）；需要维护一份与 Remotion 版本绑定的 composition。
- 关键未知（我没有证据）：autocut 中"合成型内容"在整体视频里的占比；本机的真实渲染速度（没有安装，没有实测）。

**④ agent 应该写 TSX 还是 JSON？**

- 官方对 agent 的方案是 TSX（skills 全是教 agent 写 TSX），同时承认代价：需要 13 个 skill、版本绑定、持续维护，且 skill 自身也有错（§3.4）；SaaS 场景里官方还要加上输入校验、清洗、自纠错重试和沙箱（§3.1）。
- 对我们的含义：若让 agent 写 JSON，规则面缩小到"schema + 单位 + 字幕时钟"，校验在静态层完成，错误可在渲染前拦截；若需要 Remotion 级别的自由度，可用 Recorder 式混合：**固定 composition 代码 + agent 只改 JSON**，并只在确有模板不覆盖的效果时开放 TSX 逃生口（这是一个设计选项，不是 Remotion 的结论）。
- 数据点：官方 SaaS 模板的"自纠错重试"侧面说明 agent 写 TSX 的一次通过率不是 100%（官方没有给出具体比例，**未找到证据**）。

---

## 附：主要来源索引

- 仓库与许可：[remotion-dev/remotion](https://github.com/remotion-dev/remotion)、[LICENSE.md](https://github.com/remotion-dev/remotion/blob/main/LICENSE.md)、[License FAQ](https://www.remotion.dev/docs/license/faq)、[remotion.pro 定价](https://www.remotion.pro/license)、[PR #3750](https://github.com/remotion-dev/remotion/pull/3750)
- AI：[ai](https://www.remotion.dev/docs/ai/)、[skills](https://www.remotion.dev/docs/ai/skills)、[mcp](https://www.remotion.dev/docs/ai/mcp)、[coding-agents](https://www.remotion.dev/docs/ai/coding-agents)、[generate](https://www.remotion.dev/docs/ai/generate)、[dynamic-compilation](https://www.remotion.dev/docs/ai/dynamic-compilation)、[ai-saas-template](https://www.remotion.dev/docs/ai/ai-saas-template)、[system-prompt.txt](https://www.remotion.dev/system-prompt.txt)、[llms.txt](https://www.remotion.dev/llms.txt)、[remotion-dev/skills](https://github.com/remotion-dev/skills)
- 渲染 / 性能：[video-tags](https://www.remotion.dev/docs/video-tags)、[performance](https://www.remotion.dev/docs/performance)、[shared-memory-capture](https://www.remotion.dev/docs/shared-memory-capture)、[chrome-headless-shell](https://www.remotion.dev/docs/miscellaneous/chrome-headless-shell)、[lambda](https://www.remotion.dev/docs/lambda)、[compare-ssr](https://www.remotion.dev/docs/compare-ssr)、[flickering](https://www.remotion.dev/docs/flickering)
- 关键 issue：[#11911](https://github.com/remotion-dev/remotion/issues/11911)、[#4300](https://github.com/remotion-dev/remotion/issues/4300)、[#4664](https://github.com/remotion-dev/remotion/issues/4664)、[#12680](https://github.com/remotion-dev/remotion/issues/12680)、[#12633](https://github.com/remotion-dev/remotion/issues/12633)、[#11704](https://github.com/remotion-dev/remotion/issues/11704)、[skills#19](https://github.com/remotion-dev/skills/issues/19)
