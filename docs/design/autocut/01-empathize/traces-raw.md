# Empathize 阶段 · 既有痕迹原始笔记（traces-raw）

采集日期：2026-10-28。方法：公开网页文本（WebSearch / WebFetch）、GitHub API（issues / discussions 的文本）、Hacker News Algolia API。未下载任何视频、图片或安装包。

本文件只做原始记录：不聚类、不计数、不提设计方案。

**范围（2026-10-28 扩展）**：最初覆盖口播/讲课/屏幕录制的去停顿粗剪；需求方随后要求覆盖全部视频类型，新增：游戏实况配音、直播切片、混剪、原创知识视频生成（TTS/配图/字幕）、鬼畜/YTPMV，以及"爆款判断与复盘"（第 5 节）。新增来源为 S55–S89，Sources 表新增"视频类型"列。

## 采集说明与局限（先读）

- **未能访问**：Reddit 全站（搜索引擎抓取被拒；直接请求 `.json` 返回 403，`old.reddit.com` 返回 302 跳转、无正文，到此为止，不再绕行）、B 站专栏/opus（返回验证码页）、Blackmagic 论坛（403）、Capterra（403）；知乎/V2EX/少数派（检索未命中相关帖子）。因此本文**没有 Reddit 与 B 站的第一手痕迹**，不要把它当作"Reddit 上用户说了什么"的证据。
- 部分页面经 WebFetch 的小模型摘要后读取（S04 第 1 页、S05、S29、S44、S45、S46、S50、S51、S52、S53 等）。引文按摘要给出，进入 Define 阶段前建议对关键引文回到 URL 复核。
- S27、S28、S29 是 Hacker News 评论，评论者不具名；其中不少是**观众视角**而非创作者视角，已在情境中标明。
- S49–S51 为中文博客/媒体，S51 与 S50 带明显推广倾向，只当"人们如何描述这个问题"的痕迹，不当作独立测评。
- 隐私：不记录个人账号名；维护者统一写"项目维护者"；Trustpilot 评论者、被媒体采访的 UP 主/创作者一律不具名。
- **新增类型的额外局限**：Reddit/B 站依旧不可达，所以游戏实况、直播切片、混剪、鬼畜、爆款复盘里**没有 Reddit、B 站、抖音、小红书的第一手创作者发言**；萌娘百科条目是前端渲染页，抓不到正文；Capterra/G2/Blackmagic 论坛 403。证据主要来自 Trustpilot 评论、GitHub issue、HN、论坛、媒体采访、官方帮助页。
- **爆款判断与复盘的证据偏弱**：能抓到的"创作者怎么说"大多是经验帖/厂商博客，缺少带真实后台数据的第一手复盘；这类来源在类型列标"营销"或"经验帖"，第 5 节每条都带来源性质。
- Trustpilot 页面只展示每个产品的前一页评论（约 20–24 条），且偏向投诉；不代表整体口碑。

---

## 1. Sources

| ID | 类型 | 视频类型 | 工具/产品 | 用户类型 | 日期 | URL |
| --- | --- | --- | --- | --- | --- | --- |
| S01 | 厂商反馈论坛 | 口播/播客（文本剪辑） | Descript（Select Silence Threshold） | 用 Descript 剪口播/播客的用户 | 2025-06-11（评论至 2026-06-26） | https://feedback.descript.com/feature-requests/p/select-silence-threshold |
| S02 | 厂商反馈论坛 | 口播/播客（文本剪辑） | Descript（Filler words 请求，604 票） | 播客主、公司内容团队、非英语（德语）播客 | 2020-07-18（评论至 2026-09） | https://feedback.descript.com/feature-requests/p/filler-words-dont-ignoreremove-unless-they-can-be-cleanly-removed |
| S03 | 厂商反馈论坛 | 口播/播客（文本剪辑） | Descript（Smoother transitions） | Descript 视频用户 | 2026-04-15 | https://feedback.descript.com/feature-requests/p/smoother-transitions-when-removing-filler-words-from-script |
| S04 | 开源项目论坛 | 口播/旁白 | Shotcut（Automatic Silence Removing） | 口播/旁白视频剪辑者、业余剪辑者 | 2022-03-13（回复至 2026-10-12） | https://forum.shotcut.org/t/automatic-silence-removing/32775 （第 2 页：`?page=2`） |
| S05 | NLE 厂商社区 | 讲课/口播 | Premiere Pro Ideas（Automatically remove silence） | 剪讲课视频的用户、谈客户需求的剪辑师 | 2023-02-14（回复至 2025-08-30） | https://community.adobe.com/t5/premiere-pro-ideas/automatically-remove-silence-from-a-video/idi-p/13577633 |
| S06 | NLE 厂商社区 | 口播/对话（文本剪辑） | Premiere Pro Ideas（No "UM" in transcription） | 用文本剪辑的 Premiere 用户 | 2023-07-24（回复至 2024-01-20） | https://community.adobe.com/t5/premiere-pro-ideas/no-quot-um-quot-in-transcription-for-text-based-editing/idi-p/13957040 |
| S07 | NLE 厂商社区 | 口播/对话（文本剪辑） | Premiere Pro（P: Filler Words not found） | 用文本剪辑的 Premiere 用户 | 2024-10-29（回复至 2025-01） | https://community.adobe.com/t5/premiere-pro-discussions/p-filler-words-not-found/td-p/15073046/highlight/true/page/2 |
| S08 | GitHub issue | 播客 | auto-editor #582 | 播客（静态画面、俄语） | 2024-10-31 | https://github.com/WyattBlue/auto-editor/issues/582 |
| S09 | GitHub issue | 屏幕录制 | auto-editor #478 | 屏幕录制（OBS .mkv）用户 | 2024-06-25 | https://github.com/WyattBlue/auto-editor/issues/478 |
| S10 | GitHub issue | 通用（含竖屏） | auto-editor #195 | 导出 Premiere 的用户（含竖屏） | 2021-06-19 | https://github.com/WyattBlue/auto-editor/issues/195 |
| S11 | GitHub issue | 屏幕录制 | auto-editor #12 | 屏幕录制（可变帧率）用户 | 2020-06-02 | https://github.com/WyattBlue/auto-editor/issues/12 |
| S12 | GitHub issue | 屏幕录制 | auto-editor #58 | OBS 录制会议/教程屏幕的用户 | 2020-07-31 | https://github.com/WyattBlue/auto-editor/issues/58 |
| S13 | GitHub issue | 屏幕录制 | auto-editor #94 | OBS 录制笔记本屏幕的用户 | 2020-11-07 | https://github.com/WyattBlue/auto-editor/issues/94 |
| S14 | GitHub issue | 通用 | auto-editor #157 | 命令行用户 | 2021-03-17 | https://github.com/WyattBlue/auto-editor/issues/157 |
| S15 | GitHub issue | 通用 | auto-editor #188 | 导出 Resolve 的用户 | 2021-06-12 | https://github.com/WyattBlue/auto-editor/issues/188 |
| S16 | GitHub issue | 通用 | auto-editor #281 | 通用用户（输出拖动卡顿） | 2022-05-13 | https://github.com/WyattBlue/auto-editor/issues/281 |
| S17 | GitHub issue | 长视频/通用 | auto-editor #269 | 处理约 3 小时长视频的用户 | 2022-04-26 | https://github.com/WyattBlue/auto-editor/issues/269 |
| S18 | GitHub issue | 长视频/通用 | auto-editor #431 | 3 小时视频导出 Premiere 的用户 | 2024-02-12 | https://github.com/WyattBlue/auto-editor/issues/431 |
| S19 | GitHub issue | 长视频/通用 | auto-editor #424 | 导出 Resolve 的用户 | 2024-02-04 | https://github.com/WyattBlue/auto-editor/issues/424 |
| S20 | GitHub issue | 长视频/通用 | auto-editor #440 | Premiere 用户 | 2024-02-24 | https://github.com/WyattBlue/auto-editor/issues/440 |
| S21 | GitHub issue | 通用 | auto-editor #373 | Windows 桌面 app 用户 | 2023-09-01 | https://github.com/WyattBlue/auto-editor/issues/373 |
| S22 | GitHub discussion | 通用 | auto-editor #1024 | 追求快节奏的创作者 | 2026-03-14 | https://github.com/WyattBlue/auto-editor/discussions/1024 |
| S23 | GitHub discussion | 屏幕录制 | auto-editor #784 | macOS M1 + OBS 屏幕录制用户 | 2025-07-08 | https://github.com/WyattBlue/auto-editor/discussions/784 |
| S24 | GitHub discussion | 通用 | auto-editor #841 | 只想处理部分时间段的用户 | 2025-10-12 | https://github.com/WyattBlue/auto-editor/discussions/841 |
| S25 | GitHub discussion | 多轨录制 | auto-editor #736 | 多音轨录制（6 轨）用户 | 2025-05-14 | https://github.com/WyattBlue/auto-editor/discussions/736 |
| S26 | GitHub discussion | 通用 | auto-editor #956 | 想批量处理多个视频的用户 | 2026-01-25 | https://github.com/WyattBlue/auto-editor/discussions/956 |
| S27 | HN 评论 | 屏幕教程/口播（含观众评论） | Recut（Show HN，Tauri 版） | screencast 创作者、YouTuber、观众 | 2022-06-16 | https://news.ycombinator.com/item?id=31767830 |
| S28 | HN 评论 | 屏幕教程/口播（含观众评论） | Recut（Show HN，Mac 原生版） | screencast / YouTube / 播客创作者 | 2021-03-02 | https://news.ycombinator.com/item?id=26317265 |
| S29 | HN 评论 | 口播/播客/教育 | Descript（Launch HN） | 做播客/视频的用户、教育场景 | 2021-01-05 | https://news.ycombinator.com/item?id=25641205 |
| S30 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #36 | 中文视频创作者（变帧率录制） | 2022-11-15 | https://github.com/mli/autocut/issues/36 |
| S31 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #15 | 国内非 Python 用户 | 2022-11-10 | https://github.com/mli/autocut/issues/15 |
| S32 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #84 | 刚开始做视频的新手 | 2023-03-21（评论至 2024-10-16） | https://github.com/mli/autocut/issues/84 |
| S33 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #45 | 想把粗剪结果导入 FCP/Premiere/Resolve 的用户 | 2022-11-18（评论至 2023-04-06） | https://github.com/mli/autocut/issues/45 |
| S34 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #111 | 中文用户（无人声音频测试） | 2023-11-01 | https://github.com/mli/autocut/issues/111 |
| S35 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #57 | Windows + 低端显卡用户 | 2022-11-29 | https://github.com/mli/autocut/issues/57 |
| S36 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #21 | 中文用户（macOS） | 2022-11-11 | https://github.com/mli/autocut/issues/21 |
| S37 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #39 | iPhone 竖屏拍摄用户 | 2022-11-16（评论至 2024-10-06） | https://github.com/mli/autocut/issues/39 |
| S38 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #22（Roadmap） | 项目贡献者、潜在用户 | 2022-11-11 | https://github.com/mli/autocut/issues/22 |
| S39 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #11 | 录制编程/软件讲解视频的用户 | 2022-11-09 | https://github.com/mli/autocut/issues/11 |
| S40 | GitHub issue（中文） | 中文口播/讲解/会议录屏 | mli/autocut #7 | 腾讯会议录屏用户 | 2022-11-08 | https://github.com/mli/autocut/issues/7 |
| S41 | GitHub issue | 屏幕录制 | carykh/jumpcutter #104 | OBS 录制用户 | 2019-05-07 | https://github.com/carykh/jumpcutter/issues/104 |
| S42 | GitHub issue | 屏幕录制 | carykh/jumpcutter #159 | 中文用户、长视频用户 | 2020-03-17 | https://github.com/carykh/jumpcutter/issues/159 |
| S43 | GitHub issue | 通用 | mifi/lossless-cut #1334 | 想按静音切分音视频的用户 | 2022-10-06（评论至 2023-11-28） | https://github.com/mifi/lossless-cut/issues/1334 |
| S44 | 开源项目论坛 | 口播/通用 | KDE 论坛 / Kdenlive（Auto-Cut Silent Parts） | 剪视频的 Kdenlive 用户 | 2025-02-26 | https://discuss.kde.org/t/feature-suggestion-auto-cut-silent-parts/30721 |
| S45 | 开源项目论坛 | 口播/通用 | KDE 论坛 / Kdenlive（AI-assisted Auto Cut） | 想要类 Descript 流程的 Kdenlive 用户 | 2026-08-15 | https://discuss.kde.org/t/ai-assisted-prompt-based-auto-cut-flow-cut-in-kdenlive/49410 |
| S46 | 开源项目论坛 | 音频 | Audacity 论坛（Truncate Silence） | 做听力跟读练习材料的音频编辑者 | 2018-05-24（回复至 2025-03） | https://forum.audacityteam.org/t/how-to-remove-silences-which-are-shorter-than-x-seconds/49237 |
| S47 | 个人博客 | 口播（talking head） | ffmpeg + MoviePy 脚本 | 做 CS 论文讲解的 YouTuber（talking head） | 2020-08-21 | https://vivekhaldar.com/articles/automatically-edit-out-silence-dead-air-from-videos |
| S48 | 个人博客 | 口播（talking head） | jumpcutter / auto-editor | 偶尔做视频的开发者 YouTuber | 2021-03-04（更新 2026-09-17） | https://flaviocopes.com/cut-silence-videos.md |
| S49 | 个人博客（中文，CSDN） | 口播（中文） | auto-editor 整合包 | 做口播粗剪的中文用户 | 2025-06-29 | https://adg.csdn.net/69532e7c5b9f5f31781bb5ec.html |
| S50 | 媒体评测（中文，36氪） | 口播（中文） | 剪映"智能剪口播" | 口播创作者（泛指） | 2026-09-07 | https://eu.36kr.com/zh/p/3973186277011715 |
| S51 | 营销：厂商向横评（中文，CSDN/AtomGit） | 口播（中文） | 鲸剪 WhaleClip / Premiere / 剪映 / Descript / Opus Clip | 批量产出口播的团队 | 2026-06-13 | https://gitcode.csdn.net/6a2c8a86662f9a54cb7de8ec.html |
| S52 | 产品评测 | 口播（talking head） | Gling | 做 talking-head 的创作者 | 2024-08-19（更新 2026-03-30） | https://www.unite.ai/gling-ai-review |
| S53 | 用户评价（AppSumo） | 教育/屏幕分享 | TimeBolt | YouTube / 教育视频 / 屏幕分享视频创作者 | 2022-03 至 2022-05 | https://appsumo.com/products/timebolt/reviews/ |
| S54 | GitHub issue | 通用 | auto-editor #400 | 想要无损剪切（流复制）的用户 | 2023-11-21 | https://github.com/WyattBlue/auto-editor/issues/400 |
| S55 | 论坛 | 游戏实况 | OBS 论坛（Trouble with recording multiple audio tracks） | 用 OBS 同时录游戏声与麦克风的录制者 | 2024-01-24 | https://obsproject.com/forum/threads/trouble-with-recording-multiple-audio-tracks.172759/ |
| S56 | 论坛 | 游戏实况 | OBS 论坛（separating audio tracks in editing software） | 用应用音频捕获分开 Discord/Spotify/游戏声的录制者 | 2024-03-02 | https://obsproject.com/forum/threads/separating-audio-tracks-in-editing-software.173708/ |
| S57 | 经验帖（中文，网易号） | 游戏实况/解说 | EV 录屏 / QQ 录屏 / CoolEdit / 剪映 / PR | 个人自媒体（怀旧游戏解说） | 2026-08-06 | https://m.163.com/dy/article/L3L9IKH60521RJM3.html |
| S58 | 产品评测 | 游戏实况/直播切片 | OpusClip 对游戏内容的评测 | 游戏内容创作者（评测者转引他人经验） | 2026-04-22 | https://www.twoaveragegamers.com/opusclip-review-2026-great-tool-wrong-hype-for-gamers/ |
| S59 | 媒体报道（中文） | 直播切片（带货） | 手机剪辑 + 多账号矩阵 / AIGC 切片 | 带货直播切片创作者、工作室、品牌 | 2023-06-28 | https://www.woshipm.com/it/5856027.html |
| S60 | 媒体报道（中文，含采访） | 直播切片（娱乐/带货） | 切片矩阵 / AI 切片服务 | 早期切片制作者、切片公司员工、矩阵运营者 | 2025-01-08 | https://www.jiemian.com/article/12223922.html |
| S61 | GitHub issue（中文） | 直播录播/自动切片 | timerring/bilive #223 | B 站/斗鱼录播用户 | 2025-03-20 | https://github.com/timerring/bilive/issues/223 |
| S62 | GitHub issue（中文） | 直播录播 | timerring/bilive #227 | 长时直播录播用户（VPS 资源受限） | 2025-03-23 | https://github.com/timerring/bilive/issues/227 |
| S63 | GitHub issue（中文） | 直播录播/自动切片 | timerring/bilive #186 | 录播 + 自动切片用户 | 2025-03-10 | https://github.com/timerring/bilive/issues/186 |
| S64 | 用户评价（Trustpilot） | 直播切片（Twitch/YouTube） | Eklipse | 主播、游戏 clip 创作者 | 2026-01 至 2026-03（页面含一条标注 2026-09 的评论） | https://uk.trustpilot.com/review/eklipse.gg?page=8 |
| S65 | 用户评价（Trustpilot） | 长视频转 shorts/播客切片 | Opus Clip | 播客、YouTube 创作者、营销人员 | 2026-03 至 2026-10 | https://www.trustpilot.com/review/opus.pro |
| S66 | 用户评价（Trustpilot） | 长视频转 shorts | Vizard | 网络研讨会、播客、访谈再利用者 | 2024-08 至 2026-09 | https://www.trustpilot.com/review/vizard.ai |
| S67 | 营销（工具 README，中文） | 直播切片 | HotClip（开源） | 工具作者自述的目标用户 | 采集日 2026-10-28 的 README | https://github.com/xixihhhh/hotclip |
| S68 | 经验帖（个人专栏，中文） | 混剪/AI 成片（B 端） | 剪映营销成片/图文成片、即创一键成片/高阶成片、智能创作云 | 游戏行业 B 端产品从业者对 AI 剪辑工具的测试 | 2024-07-29 | https://www.woshipm.com/it/6090618.html |
| S69 | NLE 厂商社区 | 混剪/竖屏重构 | Premiere Pro Auto Reframe | 把 16:9 转 9:16 的剪辑者 | 2023-01-29（回复至 2025-10-30） | https://community.adobe.com/t5/premiere-pro-discussions/premier-pro-auto-reframe-no-longer-working/td-p/13537018 |
| S70 | 开源项目论坛 | 鬼畜/YTPMV（对拍） | Shotcut 论坛（How to make a YTPMV in Shotcut） | 想做 YTPMV 的新手与回复者 | 2024-08-25 | https://forum.shotcut.org/t/how-to-make-a-ytpmv-in-shotcut/45553 |
| S71 | 营销（厂商博客） | 混剪/卡点 | CapCut Auto Cut / beat sync | 用 Auto Cut 卡点的创作者（厂商转述） | 2026-09-15 | https://www.seedance.tv/blog/capcut-auto-cut-not-working |
| S72 | HN 评论 | 原创知识/讲解视频（AI 生成） | Golpo（Launch HN） | 开发者、教育内容作者、潜在用户 | 2025-08-13 | https://news.ycombinator.com/item?id=44891090 |
| S73 | 用户评价（Trustpilot） | 原创知识视频（脚本转视频） | Pictory | 博客/脚本转视频的用户、教师 | 2025-12 至 2026-10 | https://www.trustpilot.com/review/pictory.ai |
| S74 | 用户评价（Trustpilot） | 原创知识视频（AI 生成） | InVideo | AI 生成视频的付费用户 | 2025-12 至 2026-10 | https://www.trustpilot.com/review/invideo.io |
| S75 | 营销（厂商相邻评测） | 原创知识视频（脚本转视频） | Pictory 评测 | 评测者（转引第三方测试） | 2026-06-29 更新 | https://fast.io/resources/pictory-ai-review-2026/ |
| S76 | 营销（教程，中文） | 原创知识视频（图文成片） | 剪映「图文成片」 | 新手创作者 | 2026-06-23 | https://www.aitoollab.cn/articles/jianying-ai-tutorial-2026/ |
| S77 | 媒体报道 | AI/模板化批量视频（平台政策） | YouTube Partner Program 2025-07 政策更新 | faceless/拼接类频道创作者 | 2025-07-18 | https://digiday.com/media/youtubes-ai-slop-crackdown-has-creators-concerned-marketers-cheering/ |
| S78 | 媒体报道（中文，含采访） | 鬼畜 | 鬼畜区制作与 AI 工具 | 受访 UP 主、前创作者 | 2025-03-03 | https://www.woshipm.com/it/6187131.html |
| S79 | GitHub issue | 鬼畜/YTPMV | otomad/OtomadHelper #3 | Vegas 用户、YTPMV 制作者 | 2022-07-07 | https://github.com/otomad/OtomadHelper/issues/3 |
| S80 | GitHub issue | 鬼畜/YTPMV | otomad/OtomadHelper #103 | 新手 YTPMV 制作者 | 2026-01-24 | https://github.com/otomad/OtomadHelper/issues/103 |
| S81 | GitHub README + issue 标题 | 鬼畜/YTPMV | Cantersoft/YTPMVE | Vegas Pro 上做 YTPMV 的用户 | 采集日 2026-10-28 | https://github.com/Cantersoft/YTPMVE |
| S82 | 媒体报道（中文） | 鬼畜 | 鬼畜编辑器（App） | 鬼畜创作者（媒体叙述） | 2021-08-16 | https://tech.ifeng.com/c/88kUQc5f3me |
| S83 | 平台官方帮助页 | 爆款判断与复盘（YouTube 长视频） | YouTube Studio 关键时刻留存 | YouTube 创作者 | 采集日 2026-10-28 | https://support.google.com/youtube/answer/9314415 |
| S84 | 平台官方帮助页 | 爆款判断与复盘（YouTube 长视频） | YouTube Studio 观众留存报告 | YouTube 创作者 | 采集日 2026-10-28 | https://support.google.com/youtube/answer/1715160 |
| S85 | 厂商官方文档 | 爆款判断与复盘（切片） | OpusClip Virality Score | Opus Clip 付费用户 | 采集日 2026-10-28 | https://help.opus.pro/docs/article/virality-score |
| S86 | 经验帖（专栏，中文） | 爆款判断与复盘（短视频） | 短视频黄金 3 秒 | 外贸营销从业者 | 2025-02-19 | https://www.woshipm.com/operate/6181785.html |
| S87 | 经验帖（专栏，中文） | 爆款判断与复盘（短视频） | AI 辅助复盘 | 自称上市公司 MCN 短视频负责人 | 2026-08-18 | https://www.woshipm.com/ai/6448583.html |
| S88 | 营销（厂商博客） | 爆款判断与复盘（YouTube 长视频） | AIR Media-Tech 留存剪辑指南 | YouTube 创作者（厂商向） | 2026-09 更新 | https://air.io/en/youtube-hacks/advanced-retention-editing-cutting-patterns-that-keep-viewers-past-minute-8 |
| S89 | 营销（自媒体推广文，中文） | 爆款判断与复盘（短视频） | 完播率与开头 | 泛指短视频创作者 | 2022-02-10 | https://www.163.com/dy/article/GVRVRAD90552BXQE.html |

---

## 2. Raw notes

- [S01] (paraphrase) 想像滑块一样自己设"什么算静音"，例如"低于 -40dB 就当静音"，因为目前没法控制静音阈值。 — 在 Descript 里清理口播停顿的用户
- [S01] (paraphrase) 用"缩短词间间隔"时句尾被削掉，要手动修；改成拉长间隔词不丢了，但句间停顿变得比想要的更长、节奏变慢。 — 想收紧节奏又不想伤词
- [S01] (quote) "I'm having this SAME problem. The app cuts off whole phrases when using the shorten word gap tool." — 另一位用户一年后附议
- [S01] (suggestion) 提供用户可设的静音阈值（dB）。 — 同上
- [S02] (quote) "When removing filler words like ums, uhs, etc the cuts are so harsh and abrupt it makes for such a poor audio experience." — 去掉 um/uh 后听感生硬
- [S02] (quote) "Totally agree I would rather have a filler word kept than an abrupt removal." — 宁可留着口头禅也不要突兀的剪切
- [S02] (paraphrase) 一位德语播客主说移除后常残留被删填充词的碎片；每期有 100 多个填充词，逐个手动微调就让这个功能失去意义。 — 非英语播客
- [S02] (quote) "We can no longer trust Descript for quality output." — 一位用户在"移除重复词"并入 Remove Retakes 的更新后说，该更新让他们公司没法再用
- [S02] (paraphrase) 缩短词间间隔应当尊重标点：句号后的停顿不该被缩掉。该用户花了很多小时手动修复缩短后的间隔，并自己做了批量加停顿/删填充词的工具。 — 重度播客剪辑
- [S02] (suggestion) 不要把词整段剪掉，而是移除词的同时保留背景音，像 AI 音乐分离那样。 — 想要无缝移除
- [S02] (paraphrase) 一位播客用户指出这条请求票数比路线图上其他请求多约 400 票，仍长期停在 "Under Consideration"。 — 对优先级不满
- [S02] (paraphrase) 有用户想在"裁剪并移除静音"里加入笑声检测。 — 访谈/播客
- [S02] (paraphrase) 厂商 2025-06 回应：给 Remove filler words 加了 "Avoid harsh cuts" 开关；一位用户随后称这是产品里最好的新增，但 2026-09 又有德语用户反映仍有残片。 — 厂商回应与用户反馈
- [S03] (quote) "Currently, when I remove these words, the resulting video cut feels abrupt and choppy." — 视频用户去填充词后画面跳
- [S03] (quote) "I never do the remove all feature for 1) filler words or 2) Shorten word gaps." — 一位用户不敢用"全部移除"，宁可逐个处理
- [S03] (quote) "This happens a LOT." — 另一用户附议
- [S04] (quote) "automatically make cuts in our video wherever the audio is below a certain point." — 剪语音/旁白视频、编程知识有限的用户想省时间
- [S04] (paraphrase) 希望默认约 -30dB 的阈值外加最短时长，让词间的短停顿被保留；切出来的段落仍保持为独立片段，这样还能顺手删掉重拍。 — 口播 + 重说
- [S04] (quote) "How silent does it need to be, and for how long?" — 新用户先问阈值和时长怎么定
- [S04] (paraphrase) 变通：先在 Audacity 里修剪静音，再把音频导回。 — 没有内置功能的剪辑者
- [S04] (paraphrase) 变通：把 auto-editor 的结果导出为 Shotcut 工程继续编辑；但这位用户说导出不稳定、慢，"often cuts two frames to the right"。 — 用 CLI 工具 + NLE 微调
- [S04] (paraphrase) 有人整理了约 23 个 Python 静音去除工具，只有 auto-editor 与另一个能输出 Shotcut 可编辑的 MLT 工程，其余都要重编码。 — 想保留在 NLE 里可继续编辑
- [S04] (paraphrase) 反对声：这类"magic feature"超出视频编辑器范围；也有人认为自动化"not reliable enough"，自动化越少越好。 — 偏好手动控制的用户
- [S04] (paraphrase) 另一位用户不想删任何内容，只想自动根据画面不连续生成切点（"I don't actually want anything removed, just the cuts."）。 — 不同的"自动切点"需求
- [S04] (paraphrase) 一位前程序员认为这对 talking-head 有用但很少需要，更想要能找出"既无声也无画面"的空隙（可能是剪辑失误）。 — 时间线检查
- [S04] (paraphrase) 首个请求三年多后（2025-10、2026-05、2026-10）仍有人来问是否已实现。 — 长期未满足的需求
- [S05] (paraphrase) 剪讲课视频要手动找长停顿，但短的戏剧性停顿要保留。 — 讲课视频剪辑
- [S05] (quote) "look through the waveform for obvious silences" — 现在的做法：肉眼扫波形
- [S05] (paraphrase) 希望按"音量阈值 + 最短静音时长"删静音，删下来的切片可放到新时间线或标记出来，剪口可选直切或加交叉淡化。 — 想自己决定衔接方式
- [S05] (quote) "asking software to avoid jumpcuts is probably beyond even the best A.I. today." — 提问者并不指望 AI 消灭跳切，只要它标出静音并留好 padding，剩下创意自己做
- [S05] (paraphrase) 客户常要求"没有死气"的原始采集素材，这个功能能把几小时的活缩到几分钟（"turn several hours' work into a few minutes"）。 — 为客户剪辑
- [S05] (quote) "If you have FFMPEG installed, you can delete silence right now." — 社区官方人员给的变通；同时承认主持人记不住稿、没有提词器时会形成这种工作流
- [S05] (paraphrase) 变通：先生成字幕，找字幕块之间的大空隙当静音；2025-08 还有人推荐网页工具 silenceslicer，导出后载回 Premiere。 — 没有原生功能时的绕路
- [S06] (quote) "it doesn't register when someone says 'UM' in the audio transcription." — 文本剪辑里"um"没被转写，没法选中删除
- [S06] (paraphrase) 大多数 um 落在停顿里能被当停顿处理，但夹在词与词之间的 um 被漏掉。 — 对话类素材
- [S06] (quote) "It changes um or uh to and" — 另一用户：转写把 um/uh 改成了 and
- [S06] (paraphrase) 变通：用停顿检测去删 um 所在的停顿。 — 同上
- [S07] (quote) "always says there are not filler words even when they are as clear as day in the audio." — 文本剪辑搜索 filler words 无结果
- [S07] (paraphrase) 另一用户升级到 Premiere 25 后也出现同样的 "no results"，并称已持续数月。 — 多人遇到
- [S07] (paraphrase) 变通/诊断：把搜索过滤器切到 Pauses，停顿能检测到，填充词仍检测不到。 — 官方人员建议的排查
- [S07] (paraphrase) 官方社区经理称在 25.1 和 beta 里"can't reproduce"；一位社区专家用用户给的样片测试，三个明显的 "uhms" 既没被当 filler word 也没被当 pause。 — 厂商无法复现，用户看到的与厂商看到的不一致
- [S08] (quote) "the default automatic silence removal for some reason sometimes cuts words (word endings), sometimes half of the word is cut." — 画面是静态图的播客，只想剪声音
- [S08] (paraphrase) 希望每次切除后再多留 0.2–0.5 秒。 — 同上
- [S08] (quote) "I've already had several videos lose meaning because of this, stumps of different words joined together." — 另一用户附议，担心剪完意思都变了
- [S08] (paraphrase) 建议先试 --margin；另一人说 margin 没用；又被建议把音频阈值调低（如 audio:threshold=0.02）。 — 反复试参数
- [S08] (paraphrase) 用户问系统到底用什么方法判断静音，怀疑只是"低于分贝阈值"，所以会切到词。 — 想理解黑箱
- [S08] (quote) "how can I make the algorithm not look at the motion in the video at all?" — 播客画面基本静止，却被默认的画面运动判断干扰
- [S08] (quote) "using auto-editor at the moment with mediocre quality audio is impossible. it cuts whole phrases in my case." — 俄语播客用户怀疑是录音质量/噪声造成
- [S08] (paraphrase) 变通：先用 ffmpeg 的 arnndn 降噪再跑 auto-editor，略有改善但仍会切词。 — 麦克风/噪声一般的录音
- [S08] (paraphrase) 有用户希望文档把参数（尤其 --edit）讲得更清楚。 — 参数理解成本
- [S09] (paraphrase) 想删掉"时长短于 N 秒的片段"，并指向一个已有该功能的网页工具。 — OBS 屏幕录制
- [S09] (paraphrase) 维护者给了一条表达式命令；用户导出到 Resolve 得到约 1KB 的空时间线，原因是把阈值 0.05 写成了 0.5，工具没有报错。 — 导出静默失败
- [S09] (suggestion) 把这类示例写进 README。 — 同上
- [S10] (paraphrase) 导出 XML 到 Premiere 后一直显示 "media pending"。 — 导出到 NLE 微调
- [S10] (paraphrase) 维护者排查发现多音轨视频导出时部分文件定义缺失；另一用户说 XML 导入后音频变成了单声道，不分左右。 — 多轨、立体声
- [S10] (paraphrase) 竖屏 1080×1920 的视频导出的 XML 变成 1280×720。 — 竖屏拍摄
- [S11] (paraphrase) 屏幕录制是可变帧率，指定帧率后音画错位，保留了错误的片段；建议先转成固定帧率。 — 屏幕录制
- [S11] (paraphrase) 两个实例同时运行会因共用缓存目录而互相崩溃；用户自己写了 PowerShell 脚本去并行处理。 — 批量处理
- [S12] (quote) "output file is huge with 529Mb" — 65MB 的 OBS 会议录屏处理后变成 529MB
- [S12] (paraphrase) 改用 h264 后仍有 245MB；有人试出 --video_bitrate 750k + h264 才接近原体积，但选 copy 时不能设码率。 — 输出体积/重编码
- [S13] (quote) "the speech sounds robot like" — 默认把多条音轨混音，空轨混入导致音质下降
- [S13] (paraphrase) 改用 --keep_tracks_seperate 后又报 "output.mp4 was not created"，已有同名文件时则不报错但实际没写入。 — OBS 录屏，多音轨
- [S14] (paraphrase) 命令行里调 silent_threshold / min_cut_length / min_clip_length，不跑完就不知道参数合不合适，浪费时间。 — 反复试参数
- [S14] (suggestion) 想要一个简单的 GUI，能先看到粗略的剪辑结果。 — 同上
- [S14] (paraphrase) 维护者答复：GUI 以独立应用发布（一次性 $30）。 — 厂商回应
- [S15] (paraphrase) 导入 Resolve 后切点是对的，但同一个切点的音频和视频片段没有联动（选中视频时音频不跟着选）。 — 导出 Resolve
- [S15] (paraphrase) 维护者排查：Resolve 17 对旧 FCP7 格式不联动，用 FCPXML 则联动。 — 导出格式差异
- [S16] (paraphrase) 输出视频只有 1 个关键帧，来回拖动时出现静帧；用户说任何视频都这样。 — 预览输出
- [S16] (paraphrase) 变通：用 HandBrake 重编码后好了，但文件变大。 — 重编码代价
- [S17] (paraphrase) 3 小时视频处理后音画不同步，从某个版本起出现；换成 --no-seek 后好了。 — 长视频
- [S18] (paraphrase) 3 小时视频导出 Premiere 后切点落在音频中间/随机位置，在 app 里直接渲染正常；剪成 40 分钟再导出就正常。 — 长视频导出 NLE
- [S18] (paraphrase) 维护者推测：奇怪的帧率被当作时间线的 timebase，Premiere 导入时累计精度损失。 — 帧率
- [S19] (paraphrase) 导出 Resolve 后不同步；帧率是 1731213000/28881053 这样极具体的值，建议加 -r 60。 — 手机/奇怪帧率
- [S20] (paraphrase) 导出 MP4 在 Premiere 里卡顿、甚至让程序关闭，在 Windows Media Player 里也卡。 — 输出文件难以播放
- [S21] (paraphrase) 用户喜欢桌面 app 里时间线预览能即时反映设置；希望同时看到对应的命令行。 — 参数调好后想复用
- [S21] (paraphrase) 维护者拒绝："I'm trying to make the app work less like the cli (more app like)"。 — 厂商回应
- [S22] (suggestion) 允许 --margin 取负值，把句末拖尾或呼吸声"削掉"几十毫秒，使剪辑更紧。 — 想要"超快"节奏的创作者
- [S22] (quote) "'trailing silence' or breath at the end of a sentence that makes the edit feel a bit slow" — 即使阈值很高，句尾仍有气声
- [S23] (paraphrase) 28.0.2 在 Mac M1 上经常提前剪掉片段末尾的对白；用 OBS（HEVC 硬编、mkv 转 mp4）录屏。 — 屏幕录制
- [S23] (paraphrase) 回复只是问有没有试过增加 margin。 — 参数调节作为默认答案
- [S24] (quote) "keep a specific section completely untouched" — 想对其中一段完全不做静音剪切（其余段落照剪）
- [S24] (paraphrase) 答复是用 --add-in 标出保护区间。 — 同上
- [S25] (paraphrase) 6 条音轨的录制，想对其中几轨用默认阈值，另外两轨不管有没有声音都切掉；表达式写了几次才对。 — 多轨录制
- [S26] (paraphrase) 想一次处理多个视频，只处理了一个；多输入需购买许可证。 — 批量
- [S27] (paraphrase) 作者做 Recut 是为了加快自己的 screencast 剪辑：显示波形和红色静音区，旋钮调整切多紧，能实时预览结果。 — 屏幕教程创作者
- [S27] (quote) "it's the gap between re-takes, so if I cut at those points and then delete the bad takes, it saves a ton of time" — 把静音当成"重说之间的间隙"这一启发式
- [S27] (quote) "It sounds super unnatural when even the tiniest pauses are cut out." — 作者默认在每个切点两侧留约半秒
- [S27] (quote) "I feel like I'm listening to a 10 year old with ADHD when you cut out literally any pause whatsoever." — 观众视角：停顿全剪掉不舒服
- [S27] (paraphrase) 观众里也有人说习惯后把它当作"对听众的服务"；也有人说持续跳切很烦。 — 观众感受两极
- [S27] (paraphrase) 评论者指出：很多跳切其实是因为口误后退回一两句重录再拼接，比一口气读完省事，不只是去停顿。 — 创作者行为
- [S27] (paraphrase) 评论者：跳切可用 b-roll、换机位、4K 裁切放大来遮盖；用工具预先剪好等于标出了该放 b-roll 的位置。 — 遮跳切
- [S27] (paraphrase) 有人问能不能同时去掉 "uh"、"er"、"hi guys"；有人回"用 ffmpeg silenceremove 就行"，有人推荐 Descript 和一个在线工具。 — 工具选择
- [S28] (quote) "the cut list is way more useful" — 作者说导出剪切列表（XML、EDL、ScreenFlow 工程）是最先做的功能，比导出视频更有用
- [S28] (quote) "I work in video production so bitrate is an important thing-to-know before purchasing." — 购买前想知道导出是否重编码、码率多少
- [S28] (quote) "I tried the scripts first, and wrote one of my own too, but I built this because I wanted more interactivity." — 作者试过 Python 脚本，仍想要更强的交互
- [S28] (paraphrase) 观众评论者担心有人把间隔设成 0，说话被剪得"exhausting"，让人跟不上。 — 观众感受
- [S29] (quote) "The video cuts when you remove 'uh' and 'um' are too jarring for anything professional." — 评论者认为去 uh/um 的剪切太跳
- [S29] (quote) "It's useful for removing bad takes out of a longer video." — 另一评论者的用法
- [S29] (paraphrase) 有人把 Descript 当粗剪工具，最后导出到 After Effects 或 Resolve 完成（"it doesn't do anything in terms of real video editing"）。 — 粗剪后进专业 NLE
- [S29] (paraphrase) 信任顾虑：数据是否被用来改进产品、选择退出要额外付费、担心被用来做深度伪造。 — 对云端转写/声音克隆的疑虑
- [S29] (paraphrase) 一位自称研究过语音识别的评论者说，文本编辑依赖的词级时间戳是端到端模型的弱项。 — 技术判断
- [S30] (quote) "没有剪切的字幕段对应的视频段落也受到了一定的影像" — 删掉部分字幕后剪切，没删的段落也被影响
- [S30] (paraphrase) 维护者答复：可能有些片段被识别成没说话，建议检查 srt、换大模型或加 --vad；之后修复了 vad 的 bug 并默认开启。 — 误判为静音
- [S30] (paraphrase) 用户怀疑自己录制视频的变帧率（导入剪辑器时被提示需要重新压制）也有影响。 — 变帧率
- [S31] (paraphrase) 国内网络下本地安装依赖困难，希望有免装依赖的一键运行包。 — 非 Python 用户
- [S31] (suggestion) 按 md 里的标题（#）章节标记把视频输出为几个片段，用序号 + 章节名命名，方便再进剪辑软件处理。 — 想要分段交付
- [S31] (paraphrase) 想直接用其他软件生成的字幕文件；回复说可以直接在 srt 里删句子，剪切时不传 md 即可。 — 流程兼容
- [S31] (paraphrase) 问打乱字幕顺序会不会改变输出顺序；回复说目前只保证按时间顺序；另有回复说也可以加上改变顺序的逻辑，但"希望不要减出来的视频特别跳"。 — 重排 vs 保持顺序
- [S32] (paraphrase) 刚开始做视频，视频里有些小错误想去掉，想"偷懒"，问能否做到一字一断或一词一段。 — 新手
- [S32] (quote) "感觉目前这类工具的顶点就是剪映的智能剪口播，那个字级剪辑是真的很好用。" — 2024-10 的评论
- [S33] (quote) "如果可以将 autocut 粗剪的视频，导出为 XML 然后导入到具体视频软件的话流程上会更加方便。" — 粗剪后进 Premiere / Resolve / FCP 微调
- [S33] (quote) "既节省 ffmpeg 导出视频的时间，又能够避免部分内容被意外剪掉" — 附议者看重省渲染时间和避免误剪
- [S33] (quote) "这个fcpxml坑比较多，如果文件路径不对就无法成功导入。" — 有人自己写了 srt 转 fcpxml 工具，已用它剪了两期视频
- [S33] (paraphrase) 他也说过 srt 转 fcpxml 的路子"没办法支持多个视频"。 — 多素材
- [S34] (paraphrase) 无人说话的音频转写出"字幕制作人"、"Zither Harp"等文字，维护者说这是模型本身造成的，只能用 VAD 过滤无人声部分，而且出现的内容不固定。 — 空白段转写幻觉
- [S35] (paraphrase) 转录时 GPU 占用 90% 以上，剪辑时 GPU 基本为 0、CPU 60% 左右；问是不是只能换更强的 CPU。 — 渲染耗时
- [S35] (paraphrase) 维护者答复：剪辑用 ffmpeg，不支持 GPU 加速；另有人说 GPU 压制质量可能有问题。 — 同上
- [S36] (paraphrase) 一个 10 秒的小视频跑 autocut -d 等了一小时没结果；首次要下载 461MB 的模型，下了 27 分钟。 — 国内网络
- [S36] (paraphrase) 总结：small 模型转录速度可接受，large 难；但 large 中文准确率明显更高。 — 速度 vs 准确率
- [S37] (paraphrase) iPhone 竖屏拍摄的视频（1080×1920，HEVC，带旋转信息）剪辑后变成 1920×1080 的"拉扁的横视频"，2024-10 仍有人能复现。 — 手机拍摄
- [S38] (quote) "目前whisper在CPU上推理性能堪忧" — 维护者自己列在路线图第一条
- [S38] (suggestion) GUI 里要有 video panel、显示声音的 audio panel（"很方便看到是不是停顿太多"）、可编辑字幕 panel，光标换到某行时同步用 2 倍速播放，能在音频 panel 里移动来微调时间戳，"但还是想以文本字幕为主panel"。 — 想要的审阅界面
- [S38] (paraphrase) 有人建议 UI 参考 Descript。 — 参照物
- [S39] (paraphrase) 讲编程/软件的视频中间停顿少，转录出的字幕一条长达二十多秒，连成一片，想要自动断句。 — 讲解类视频
- [S39] (quote) "如果辛辛苦苦矫正的 src 文件没法被 cut 同步，后期还得再来一次字幕生成&矫正，也费时。" — 视频剪完字幕没有同步被剪
- [S39] (paraphrase) 换 large 模型断句更短，但对"视频字幕"仍偏长。 — 模型不是为配字幕微调的
- [S40] (paraphrase) 腾讯会议录屏导出时长极长，原因是输出 fps 被设成了 90000；用剪映原样导出一遍 mp4 再放进 autocut 就没问题。 — 会议录屏
- [S40] (paraphrase) 加 --vad 后生成的字幕文件只剩一小段，似乎把正常说话也去掉了；维护者说"vad 对一些视频好，但一些变差"。 — 误判静音
- [S40] (paraphrase) 在 vscode 里勾选 md 复选框写成大写 X，导致后续无法运行。 — 文本审阅格式的脆弱
- [S41] (paraphrase) 用 OBS 录制，输出画质不如原片；换不同格式（mp4/mov/mkv/ts）音频都有延迟。 — OBS 录制
- [S42] (quote) "this script will amplify my audio, which will also increase the background noise, which annoies me." — 用户抱怨处理后背景噪声变大
- [S42] (paraphrase) 路径带空格不支持；视频超过约 10 分钟会因内存不足崩溃，有人 fork 后改成存磁盘。 — 长视频
- [S43] (paraphrase) 想按静音位置切分文件；2022-12 后 LosslessCut 加入静音检测，有用户试用后无法导出（出现重叠片段）。 — 无损切割 + 静音
- [S43] (paraphrase) 2023-11 有人想把 auto-editor 的静音检测与 LosslessCut 的无损剪切结合，因为 auto-editor 不支持无损剪切。 — 避免重编码
- [S44] (quote) "Doing it manually, by looking at the waveform, is too tiring, and taking so much time." — 手动看波形剪静音
- [S44] (paraphrase) 想要：自动检测并切分静音、最短静音时长（短停顿保留）、dB 阈值、可选只切音频/视频/两者，也适用于"没有语音但有声音"的素材。 — 通用剪辑
- [S44] (paraphrase) Kdenlive 已有去除非语音段的功能，但"那是针对语音的"。 — 现有功能不够
- [S45] (paraphrase) 想找 Descript 式的"flow cut"：根据转写自动去静音、停顿、填充词，并能用提示词/规则控制。 — 想要类 Descript 的流程
- [S45] (paraphrase) 回复：Kdenlive 暂无脚本接口，只能在外部做；已有基于语音转文字的静音检测。 — 现状
- [S46] (paraphrase) 用户想只删短于约 2 秒的静音（做听力跟读练习），Truncate Silence 的时长是"最小时长"，没有"最大时长"。 — 音频编辑
- [S46] (paraphrase) 志愿者解释：必须保留一个最小时长，否则词尾和自然停顿会被削掉；变通是在要保留的间隙上叠音调轨，或用 Nyquist 插件给静音打标签再删。 — 绕路
- [S47] (quote) "one of the most time-consuming yet mindnumbingly boring parts" — 作者形容手动找出并剪掉死气（dead air）
- [S47] (paraphrase) 之前用 ffmpeg silencedetect 的脚本，它有时会漏掉长段静音，只能手动再修一遍并重新渲染。 — 召回不全
- [S48] (quote) "I always end up with a 20 minutes video that I need to edit down to 10 minutes." — 录制时多处失误和停顿
- [S48] (paraphrase) 方法对"一直在说话"的视频好用，不适合要敲代码的视频；24 分钟视频剪到 14 分钟。 — 适用范围
- [S48] (quote) "I just need to remove the takes I shoot 2 or 3 times." — 去静音之后，重拍的段落仍要手动删
- [S48] (paraphrase) jumpcutter 自 2021 年 4 月后无更新，auto-editor 不再走 pip，Linux/Windows 用户要去 GitHub 下二进制。 — 安装门槛
- [S49] (quote) "剪辑视频时手动删除视频中的无效静音片段非常的枯燥耗时" — 用 auto-editor 做口播粗剪
- [S49] (quote) "剪映不能调音量大小和静音时长" — 作者承认剪映也有该功能，但认为参数不可调
- [S49] (quote) "自动剪辑的结果感觉说活声音很局促，太生硬了" — 对剪映结果的评价
- [S49] (paraphrase) auto-editor 的参数相互依赖：音量阈值为空时选择音轨和运动阈值不生效；阈值越大剪得越多，要自己权衡。 — 参数理解
- [S49] (paraphrase) 复杂设置或长视频耗时不可预期，路径和文件名要避免非英文字符和空格。 — 使用门槛
- [S50] (paraphrase) 口播里的无用停顿和语气词"繁琐难删"，背景噪声会影响听感；长视频逐句筛选和敲字幕占去大量时间；字幕识别会频繁错字。 — 口播创作者
- [S50] (paraphrase) 传统做法被描述为逐帧剪切；"智能剪口播"自动识别口播里的无声停顿、语气词和重复句，用户在生成的文本界面上删字，视频音频同步裁切（"像修改文档一样剪视频"）。 — 文本化粗剪
- [S50] (paraphrase) 文中没有给出任何实测准确率数据。 — 来源局限
- [S51] (paraphrase) 描述传统软件处理口播要"人工逐帧剔除气口"，还要手动校对字幕时间轴；团队批量生产时"剪辑产能往往是最大的瓶颈"。 — 批量产出口播的团队
- [S51] (paraphrase) 评价：剪映识别准确率高但缺少 CLI/批处理；Descript 剔除填充词流畅但中文支持较弱；Premiere 多依赖第三方插件。 — 厂商式对比（推广性质）
- [S52] (quote) "I was surprised at how accurate Gling was in detecting and removing filler words." — 评测者体验（有联盟佣金披露）
- [S52] (paraphrase) 流程：上传到桌面 app，在转写脚本里点句子即可剪或重排；Cut 在时间线上可预览，能开关显示/跳过；播放速度 0.25–2 倍；有 Split / Pace / Zoom 工具手动细调。 — 审阅方式
- [S52] (paraphrase) 可直接导出到 Premiere、Final Cut、Resolve；评测没说明哪些内容（captions、zooms）会带过去。 — 导出到 NLE
- [S52] (paraphrase) 限制：目前仅支持英语；"lacks the features of full video editing software"。 — 范围
- [S53] (quote) "No longer am I dependent on the video editor to jump cut (badly) and take hours for producing educational content." — 教育视频创作者（2022-03）
- [S53] (quote) "I was looking for a simpler way to cut out silence." — 另一位用户，此前在 Windows 上用 Filmora，换 Mac 后没有该功能
- [S53] (quote) "I always use it after I make some screen share videos." — 录屏视频
- [S53] (paraphrase) 页面可见评价都是 5 星；批评只有界面朴素、希望有手机版。 — 样本偏正面，且只看了 5 条
- [S54] (paraphrase) 有用户写了无损（流复制）剪切 + 变速的脚本，希望进主 CLI，加 --lossless 与警告。 — 想避免重编码
- [S54] (paraphrase) 测试后发现切点与 LosslessCut 的"检测静音再剪"不完全一致，怀疑与"合并重叠片段"的逻辑有关。 — 无损切点精度

- [S55] (paraphrase) 按 OBS 指引录多音轨，得到的却是一个混合音轨的文件，Clipchamp 和 OpenShot 导入后也只剩混音流。 — 想把游戏声与麦克风分开处理的录制者
- [S55] (paraphrase) 回复解释：OBS 写出三条轨（轨 1 是混音、轨 2 只有桌面音、轨 3 只有麦克风），有的编辑器只显示混合轨；在 VLC 里必须手动切换音轨才听得到。 — 同上
- [S55] (quote) "Any modern video editor can see all the audio tracks separated." — 回复者建议换编辑器
- [S55] (paraphrase) 变通：换成免费的 DaVinci Resolve，导入后三条轨都出现了；也有人建议用 ffmpeg 单独提取每条音轨，或用 Audacity 导入。 — 绕路
- [S56] (quote) "none of the tracks imported separately." — 用 OBS 应用音频捕获分开 Discord、Spotify 和游戏声，想剪辑时静音 Discord 对话、保留游戏声，导入 Premiere 后轨道没有分开
- [S56] (paraphrase) 回复说要把每个来源手动分配到各自的音轨；该用户确认这样可行。 — 游戏实况录制者
- [S57] (paraphrase) 游戏解说的录制：用 EV 录屏或 QQ 录屏录画面，用 CoolEdit 多轨录解说（人声在录音轨、配乐放其他轨），可以分段录再拼接。 — 做怀旧游戏解说的个人自媒体
- [S57] (paraphrase) 剪辑推荐剪映：能根据解说词生成字幕，之后只需校正错别字；PR 或 Edius 也可以，但逐句加字幕很费时，作者的办法是用 PS 把 TXT 文案批量变成字幕素材，再在 PR 里按配音拖动调整时长。 — 同上
- [S57] (quote) "你的视频和字幕素材的规格必须是一样的" — 批量字幕素材的尺寸必须与视频一致，否则位置错位
- [S57] (paraphrase) 作者说很多人一句一句手动加字幕，"浪费了大量的时间"；单行字幕不宜过长，观众阅读通常至少需要 1 秒。 — 同上
- [S58] (paraphrase) 评测者以 Apex Legends 为例：工具"听"不"看"，挑出了平静讲解配装的片段，漏掉了一段没有说话的三杀 clutch；评测者写它"has no idea what's happening on your screen"（原文）。 — 游戏 clip 创作者
- [S58] (paraphrase) 评测者转引另一位创作者的说法：该工具"waits for speech pauses and cuts late, skipping the action"，但这位创作者仍用它剪播客片段。 — 游戏 vs 播客的差别
- [S58] (paraphrase) 评测者说即使是口播类内容，发布前也得逐条审阅每个 clip：AI 会"cuts before a full thought is completed"，或抓到铺垫却漏掉笑点。 — 切片的审阅习惯
- [S58] (paraphrase) 积分制不适合经常开播的主播：每周三次、每次 2 小时的直播相当于每月 24 小时内容，而 Pro 的 300 积分约合 5 小时。 — 主播的使用量
- [S58] (quote) "as a ranking tool, not a guarantee" — 评测者建议的爆款预测分（virality score）的用法：只拿来给一批 clip 排序
- [S59] (paraphrase) 带货切片：创作者（多为宝妈、学生、待业者）经授权后用手机剪辑达人的直播素材，同时运营多个账号；表现差的账号被舍弃，爆款稍作改动后反复发布，直到流量消失。 — 带货直播切片
- [S59] (quote) "眼下，只是简单把直播素材剪辑发布，已经拿不到流量了。" — 报道对切片现状的描述
- [S59] (paraphrase) 一个切片账号只能获得一个达人授权，单个达人授权封顶 5000 个账号，文中指出销售额存在"稀释摊薄"风险；平台开始管控未授权账号和内容。 — 授权与平台管控
- [S59] (paraphrase) 优质账号会加入"自己的理解"，输出更有独创性的视频，或搭配不同音乐和素材；报道里一家 AIGC 切片服务商的案例称产出切片 540 条、日均发布量提升 4 倍，但报道也说没有公布对销售额的影响。 — 报道转述，非创作者原话
- [S60] (quote) "切片矩阵的确有效果，但能产生多大效果其实取决于主播本人。" — 一位切片公司员工
- [S60] (paraphrase) 一位早期做某主播切片的制作者，起初是"为爱发电"，后来剪这个主播的人越来越多，她的流量没有提升，就不再剪了。 — 早期切片制作者
- [S60] (paraphrase) 切片矩阵运营者说公司只负责制定方案和搭建矩阵，"无法提供任何保障"；不少传媒公司买过矩阵服务，达到预期的很少。 — 矩阵运营方
- [S60] (paraphrase) 某传媒公司宣称 498000 的套餐可让 5000 个矩阵账号发布 240 万条 AI 生成的直播切片（用 AI 抓取高光、自动加花字和音效）；报道说这是销售端宣传，没有核实效果。 — 厂商宣传，来源性质为报道转述
- [S60] (paraphrase) 未经授权剪辑可能被举报、限流或封号；允许无授权剪辑后切片泛滥，也让早期真爱粉流失。 — 授权与粉丝关系
- [S61] (paraphrase) 先前用 biliup 录制斗鱼录播并上传，试用 bilive 后觉得"低端机器依然可以做弹幕+视频合并，真的很不错"；但一直没有切片产出，希望自动切片功能完善，最好免费。 — 录播/自动切片用户
- [S61] (paraphrase) 开发者回复：自动切片要用到需要 API 的模型；ARM 版本因上游依赖（含 triton）没有 ARM 版而暂缺，正在调研用便宜的 API 替代本地 whisper。 — 开发者回复
- [S62] (paraphrase) 想要"分段录完就上传、上传完删本地，主播下播后一起发布"：主播直播四个小时、一小时一段，为了省 VPS 资源。 — 长时直播录播
- [S62] (paraphrase) 开发者回复：B 站没有"先传后统一发布"的接口；分 P 上传后，每个分 P 上传成功都会让前面的分 P 变成公开。 — 平台限制
- [S63] (paraphrase) 自动生成的切片标题由大模型生成，维护者说效果"不是很稳定"；录播标题只用 mp4 元数据套模板，因为录播动辄一小时以上，上传给 API 要占大量带宽。 — 自动切片的标题
- [S63] (paraphrase) 用户问：切片没有 tag（原视频有）、能否只上传切片、一小时的录播只切出三十多秒"似乎有点短"。 — 对切片产出的疑问
- [S64] (quote) "will just cut random clips that aren't very interesting." — 一位 Eklipse 用户（2026-03）
- [S64] (quote) "The AI isn't perfect every time." — 另一位用户
- [S64] (quote) "Ai highlights with rating scores help you pick the best ones." — 正面评价：评分帮助挑选
- [S64] (paraphrase) 一位有正职的主播说"no time to edit"，用它把直播变成 clips；另一位用来把游戏 clip 发到社交媒体。 — 主播
- [S64] (paraphrase) 一条 2 星评价：编辑器卡顿、不能分享到 Instagram、不能上传已有 clip 让 AI 剪，字幕生成器"a complete miss"，手机浏览器打开的是编辑器而不是下载。 — 差评
- [S64] (quote) "Not worth the sub and you can't get a refund." — 一条 2 星评价标题
- [S65] (quote) "it does make good suggestions" — Opus Clip 用户评价片段式推荐
- [S65] (quote) "could use a better manual editing UI" — 另一用户
- [S65] (paraphrase) 差评集中在积分与扣费：约 3000 个未用积分过期、取消订阅后仍被扣款、取消后仍然反复尝试扣款。 — 付费体验
- [S65] (paraphrase) 可见评论中无人评论 virality score，也没有人提到游戏或 Twitch VOD；只有一位 YouTube 用户称"has really helped us gain more followers and more views"。 — 样本说明
- [S66] (quote) "AI editing doesn't really work" — 一位 Vizard 用户（2026-06）
- [S66] (paraphrase) 一位用户因 Opus 处理大文件时服务器变慢而转到 Vizard；另一位用户说加 outro 时背景音乐被突兀切断，剪出来显得不精致。 — 切换工具
- [S66] (paraphrase) 转录免费，但复制/导出文本要付费；取消订阅后未用积分作废。 — 付费墙
- [S67] (paraphrase) 工具自述：长视频、直播回放一键切成竖屏短视频，功能写明"AI 找高光金句、弹幕热度进爆点判断、自动加字幕、横屏转竖屏 9:16、去气口剪口头禅"，并强调"素材不上传、无积分制"。 — 工具 README（营销）
- [S67] (paraphrase) README 里把"时长档"和"最多切片数量"常驻右栏，并写明"数量是候选上限，不会为凑数降低质量"；逐句稿支持搜索、试听确认和撤销。 — 工具作者认为需要的审阅控件（营销）
- [S68] (quote) "这是个不可控的“黑盒”" — 作者评剪映"营销成片"：混剪逻辑、字体颜色、BGM 等都无法设置
- [S68] (quote) "AI生成错误的内容，想救也救不了" — 作者评即创"一键成片"：生成后无法二次修改
- [S68] (paraphrase) 作者测剪映"图文成片"：能一定程度匹配文本内容，但出现带水印素材和不知名品牌片段，有版权风险；一键成片的特效"抓得也不准"，且无法去除。 — B 端混剪
- [S68] (paraphrase) 企业可能需要"按音乐节拍混剪"、指定字体和非机械感配音，这些在当前产品里难以实现。 — 对混剪的需求
- [S68] (paraphrase) 作者判断 AI 成片"无法满足符合业务最低限度诉求"，与其逐条审核修改，不如直接用传统方式批量混剪，"也才几个小时的工作量"；在多个功能间传素材"十分耗费人力"。 — 修正成本抵消自动化收益
- [S69] (quote) "Once the subject is out of a 9:16 frame, Premier no longer auto reframes or tracks the subject." — 16:9 转 9:16 的用户（2023-01）
- [S69] (quote) "there is no generated path - that is the problem." — Reframe Offset/Scale 保持默认值，没有生成路径
- [S69] (paraphrase) 其他用户：9:16 序列里主体被移出画面；分析卡在 45%（低内存警告）或 90%；关键帧只写了开头几个；一段 50 秒口播样片里主体漂到左边；2024-08 和 2025-10 仍有人遇到分析不结束。 — 竖屏重构
- [S69] (paraphrase) 变通：取消勾选"scale to frame size"，删除 Reframe 效果再重新添加。 — 绕路
- [S70] (quote) "The hardest (most laborious) part with music videos of any kind is beat synchronization." — 回复者谈对拍
- [S70] (paraphrase) 其变通：把音乐重采样成整数拍/秒（120 BPM），项目帧率设为整数 30fps，使一拍 = 15 帧；把跳转距离设为一拍，用快捷键逐拍跳转并加关键帧。 — 对拍的手动方法
- [S70] (quote) "I would say that it would take me a big amount of time." — 另一回复者谈在 Shotcut 里做 YTPMV
- [S70] (suggestion) 希望能把检测到的节拍导出成 Shotcut 的标记（MLT 文件），可以用 AI 或简单的峰值检测。 — 同上
- [S71] (paraphrase) 厂商博客列出卡点失败的原因：弱节拍、节奏漂移、抒情长前奏、人声密集的音乐，以及有背景噪声、长呼吸或音乐与说话同音量的语音。 — 卡点/停顿检测
- [S71] (quote) "Auto Cut should provide a first assembly, not the final editorial judgment" — 厂商博客的核心建议；用户手动做的修正包括挪晚/早的切点、删重复片段、减少与节奏冲突的转场（营销来源）
- [S72] (paraphrase) 创始人自述：一条 2 分钟的视频也可能要几个小时，要策划、写稿、录制、剪辑、让声音和画面对齐。 — 做讲解视频的动机（厂商自述）
- [S72] (paraphrase) 一位评论者试用后觉得演示里"函数指针"例子的图表停留太短，新人跟不上；另一位评论者说看到第 8 秒就停了，因为代码示例"went too fast to read"，且代码本身有错。 — 讲解视频的节奏
- [S72] (paraphrase) 一位评论者为一个插秧机器人做产品讲解，发现机器人在每个场景里长得都不一样。 — 角色一致性
- [S72] (quote) "It fills the slides in erratically, even text. No human does that." — 另一位评论者批评文字随机出现、像打印不像手绘
- [S72] (paraphrase) 评论者问能否逐帧/内联编辑生成结果；创始人答复正在做逐帧编辑的 storyboard 功能。 — 想要局部修改
- [S72] (paraphrase) 一位评论者把自己开源项目的用户文档丢进去，说结果"surprisingly not terrible"；另一位说自己一直想教学但没时间，这或许是个办法。 — 文档转视频
- [S72] (quote) "Why would you need to add AI into the equation?" — 一位评论者的质疑（Khan Academy 就是雇人做视频）；创始人回："You can't have arbitrary content with a human in the workflow."
- [S72] (paraphrase) 评论者问 credit 是什么、最低档是否只有 15 秒视频；创始人答复上限是 2 分钟。 — 定价不清
- [S73] (paraphrase) Pictory 用户：把脚本和博客变成视频"in mere minutes"；有人用 API 渲染一个纪录片风格的 YouTube 频道；一位教师想做物理视频。 — 脚本/博客转视频
- [S73] (paraphrase) 可见评论里没有关于素材匹配的抱怨，也没有播放量数据；有人说"cancel subscription does not register"，续费照扣。 — 样本说明
- [S74] (quote) "The selection of footage is dreadful." — InVideo 用户（2025-12）
- [S74] (quote) "pronounce a word correctly the first time, but something totally different the second time." — 一位用户的 AI 配音问题
- [S74] (quote) "Lipsync out, wrong camera angle, chopped the sentence leaving out the last word." — 另一位用户
- [S74] (quote) "If you want to add 3-5 videos per week, this is not the tool to use." — 一位用户做完一条 30 分钟视频后无法完成第二条 15 分钟的视频
- [S74] (paraphrase) 多位用户抱怨积分消耗过快：一条未完成的 35 秒视频用光了整月积分；2300 个积分烧掉后没有成品。 — 积分制
- [S74] (quote) "50 pointless questions to answer before you can use the editor" — 一位用户对上手流程的抱怨
- [S75] (paraphrase) 评测说粘贴脚本后一分钟内就出第一版；按关键词匹配素材"hit-or-miss"，很多素材"very generic and stock clippy"；标准 AI 配音"functional but recognizably synthetic"；只支持 16:9、9:16、1:1，没有关键帧和分层合成。 — 评测（厂商相邻，转引第三方测试）
- [S76] (paraphrase) 剪映「图文成片」：粘贴最多 5000 字的文本，选音色，生成，约 30 秒；作者建议花约 5 分钟浏览并替换不匹配的素材，示例里要替换 3–5 个，再检查字幕错字（多为术语）。 — 教程（营销性质）
- [S76] (paraphrase) 作者举例：讲到"人工智能"却配了猫的图片。 — 素材不匹配
- [S76] (paraphrase) 免费版导出带水印；无水印 1080p 以上和部分音色/数字人需要 VIP。 — 付费墙
- [S77] (quote) "it's going to consider it more repetitious content." — 一位用拼接现有视频做动物故事的创作者担心被判定为重复内容
- [S77] (paraphrase) 一位曾在更早的重复内容清理中被终止频道的创作者，认为 YouTube 的做法"a bit harsh"，预计低成本的模板化 AI 视频会失去变现资格。 — 创作者反应
- [S77] (paraphrase) 创作者担心政策措辞宽泛，难以判断哪些自动化/AI 辅助的视频被允许；YouTube 官方人员称这是"minor update"，并说更新没有单独针对 AI 内容。 — 平台政策
- [S78] (paraphrase) 鬼畜制作：选人声清晰的素材 → 调音、和声、混音 → 配画面和字幕；多用重复、循环、卡点；分音MAD、人力 Vocaloid、鬼畜剧场三类。 — 媒体归纳
- [S78] (paraphrase) 一位受访 UP 主说几分钟的视频以前需要"耗时数月之久"，《派大星的独白》花了约 3–4 个月；一位前创作者说"做几个月很正常"。 — 耗时
- [S78] (paraphrase) 一位 UP 主说以前"光拼字就要30小时以上"，如今 AI 生成脚本到 demo"仅需半个小时"，对 AI 工具持接纳态度。 — 对 AI 的态度
- [S78] (paraphrase) 受访 UP 主说多数制作者"为爱发电"，从不接商单；AI 能让台词贴合音乐、更高效地尝试想法。 — 动机
- [S78] (paraphrase) 观众有分歧：有用户认为手工拼接的不协调感正是鬼畜韵味，AI 制作"没那味儿"；受访的前创作者不太愿意看 AI 批量生产的鬼畜。 — 对 AI 的态度
- [S78] (paraphrase) 文章提到 2024-12 广电总局要求排查清理 AI"魔改"影视剧的短视频。 — 政策背景
- [S79] (paraphrase) OtomadHelper（Vegas 脚本，从 MIDI 自动生成 YTPMV 轨道）用户建议像音频一样为和弦生成多条视频轨；开发者回复脚本默认已为和弦生成多轨，只需加开关。 — YTPMV 制作者
- [S80] (paraphrase) 一个 100 多个音符的通道只生成了 70 个、到第 13 小节为止，原因是没有导出整段区域；随后又遇到 MIDI 里 velocity 为 0 的音符导致 "Got an off without an on"。 — 新手上手摩擦
- [S80] (quote) "OMG THERE'S SO MANY PROBLEMS MAN" — 同一位新手
- [S81] (paraphrase) YTPMVE README：脚本按 MIDI 把 Vegas 里的素材自动对拍；使用前要把每个音频片段的 Pitch Change 调到 A4；对拍失败时建议去掉不必要的通道和特别长的音符。 — 工具文档
- [S81] (quote) "Vegas Pro is not a very good tool for audio production." — README 自己说明，并建议用 FL Studio 做更真实的 YTPMV 音频
- [S81] (paraphrase) 该仓库的 issue 标题多为安装与报错，如 "Script not working using any type of midi file"、"Can't find YTPMVE_UI.exe"（只读了标题）。 — 上手门槛
- [S82] (paraphrase) 媒体叙述：此前鬼畜创作者要依赖 PC 端软件，"流程较为繁琐，产出效率较低"；该鬼畜编辑器能对每个字的声调做调试，并对歌词文稿做分段延时处理。 — 新闻稿式报道

---

## 3. 用户/情境观察

- 屏幕录制 / 屏幕教程创作者：用 OBS 录屏（含 HEVC 硬编、mkv→mp4、多音轨），或录会议、软件讲解，然后想去掉停顿。（S09, S12, S13, S23, S27, S28, S39, S40, S41, S53）
- 播客/访谈：画面基本是静态图或固定机位，只关心声音；有英语以外语言（俄语、德语）。（S02, S08）
- talking-head YouTuber / 开发者讲解：录制时多处失误和停顿，原始 20 分钟要剪成 10 分钟，还要手动删重拍的 take。（S47, S48）
- 讲课/培训视频：手动找长停顿，保留短的戏剧性停顿；也有客户要求"无死气"的原始素材。（S05, S53）
- 观众（HN 评论）：对"全删停顿"和"连续跳切"有两极反应，也会评论创作者为什么跳切。（S27, S28）
- 中文用户：刚开始做视频的新手；国内网络安装依赖、下载模型困难；录制素材含腾讯会议录屏、iPhone 竖屏、变帧率视频。（S30, S31, S32, S36, S37, S40）
- 中文口播批量产出团队：描述"逐帧剔除气口"、产能是瓶颈（带推广性质）。（S50, S51）
- 多轨录制用户：6 条音轨，不同轨要不同阈值。（S25）
- 有人明确偏好手动控制或不信任自动化：认为自动化"not reliable enough"；不敢用"全部移除"；信任感因更新而下降。（S02, S03, S04）
- 常用 NLE / 剪辑环境：Premiere、DaVinci Resolve、Final Cut Pro、Shotcut、Kdenlive、Audacity、Descript、OBS、HandBrake、剪映、Filmora。（S04, S05, S10, S15, S28, S29, S33, S44, S46, S53）
- 高要求场景：客户交付（S05）、需要知道导出码率（S28）、专业 NLE 里继续精修（S29, S33）。
- 设备/素材特征：可变帧率屏幕录制、iPhone 竖屏、奇怪的分数帧率、3 小时长素材。（S11, S17, S18, S19, S37）

### 新增类型的用户/情境观察（2026-10-28 扩展）

- 游戏实况/解说：用 OBS 同时录游戏声、麦克风、Discord、Spotify，想在剪辑时单独静音或调整某一来源；轨道在 Premiere/Clipchamp/OpenShot 里不一定分开显示，换到 Resolve 才看到。（S55, S56）
- 游戏实况（中文）：个人自媒体用 EV/QQ 录屏录画面、用 CoolEdit 多轨录解说，再用剪映或 PR 加字幕；逐句手动加字幕被视为大量耗时。（S57）
- 游戏高光：基于语音的切片工具会漏掉"没说话但精彩"的操作；评测者仍保持每条 clip 发布前自己审阅。（S58）
- 直播切片（中文带货）：宝妈、学生、待业者与工作室，经达人授权后用手机剪辑，多账号运营，靠爆款改动重发；授权数量与平台管控限制规模；效果被从业者认为"取决于主播本人"。（S59, S60）
- 直播录播/自动切片（中文）：在 VPS 上录 B 站/斗鱼直播，加弹幕、字幕，用大模型生成切片与标题并上传；关心资源占用、上传时机、标题和 tag。（S61, S62, S63）
- Twitch/YouTube 主播：有正职、没时间剪，用 AI 把直播变成 clip；也有人把游戏 clip 发到短视频平台；对积分制、退款、字幕质量、编辑器卡顿有抱怨。（S64, S65, S66）
- 混剪/AI 成片（B 端）：企业需要按镜头组、按节拍混剪，指定字体和非机械感配音；对"黑盒"成片的可控性不满，认为修正成本抵消自动化收益。（S68）
- 竖屏重构：把 16:9 素材转 9:16 的剪辑者遇到主体丢失、分析卡住、关键帧不写入。（S69）
- 卡点/对拍：新手与回复者都认为对拍是音乐视频里最费力的部分，手动靠整数帧率与逐拍跳转加关键帧。（S70, S71）
- 原创知识/讲解视频（AI 生成）：开发者与教育内容作者想把文档、脚本、博客变成视频；用户抱怨素材匹配、配音与口型、角色一致性、积分消耗；评论者对讲解节奏（图表停留太短）有具体意见。（S72, S73, S74, S75, S76）
- AI/模板化批量视频创作者：对 YouTube 2025-07 的"inauthentic content"政策措辞宽泛感到担心。（S77）
- 鬼畜/YTPMV：多数制作者"为爱发电"，一个视频以前做数月；工具链是 Vegas + MIDI 脚本 + FL Studio 等；新手在安装、MIDI 格式（velocity 0 音符）、导出区域上频繁出错；观众对 AI 制作的鬼畜有"没那味儿"的看法。（S78, S79, S80, S81, S82, S70）

## 4. 现有工作流步骤

- 手动：肉眼扫波形找停顿再逐段切（S05, S44）；或生成字幕后找字幕块之间的空隙（S05）。
- 命令行脚本：ffmpeg silencedetect / silenceremove，再用 MoviePy 渲染；会漏长静音，需手动补（S47, S27）。
- jumpcutter：`--silent_speed 999999 --frame_margin 2 --frame_rate 30`；之后重拍的 take 仍需手动删（S48, S41, S42）。
- auto-editor：命令行调 margin / threshold，不满意再重跑；结果导出为 XML / FCPXML / MLT 工程到 Premiere、Resolve、Shotcut 继续微调（S08, S10, S15, S04, S09, S49）。
- Recut：把切得很紧的结果当作"重说之间的间隙"，先切再删坏 take；可导出 XML / EDL / ScreenFlow 工程（S27, S28）。
- Audacity：用 Truncate Silence 或 Nyquist 插件给静音打标签再删（S46, S04）。
- Descript：转写文本里删词、Remove filler words、Shorten word gaps；用户倾向逐个处理而不是"全部移除"；粗剪后导出到 After Effects / Resolve 完成（S01, S02, S03, S29）。
- Premiere 文本剪辑：转写后搜索 filler words 或 Pauses；填充词检测失败时改用 Pauses 过滤器（S06, S07）。
- Gling：上传 → 转写脚本 → 自动去 bad takes / 静音 / 填充词 → 在转写编辑器里开关 cut 预览 → Split/Pace/Zoom 手动细调 → 导出到 Premiere / Final Cut / Resolve（S52）。
- TimeBolt：先去静音，之后再去剪辑软件（S53）。
- mli/autocut：`autocut -t` 转录 → 在 md / srt 里勾选或删字幕 → `autocut -c` 按字幕剪切；也可只传 srt（S30, S31, S38）。
- 剪映"智能剪口播"：识别口播里的停顿、语气词和重复句，在文本界面删字（S50, S32）。
- 素材预处理变通：HandBrake 重编码修复关键帧/卡顿（S16）；用剪映原样导出一遍再喂工具（S40）；先用 ffmpeg arnndn 降噪（S08）；先转成固定帧率（S11）；`--no-seek`（S17）；加 `-r 60`（S19）。
- 用 Audacity 等外部工具先处理音频再回导（S04）；用 b-roll / 换机位 / 4K 裁切遮盖跳切（S27）。

### 新增类型的现有工作流步骤（2026-10-28 扩展）

- 游戏实况（英文社区）：OBS 里把每个音频来源手动分配到各自音轨 → 录制 → 导入 Resolve/Premiere 分轨剪辑（Premiere 与 Clipchamp、OpenShot 有时只显示混合轨）；备选是用 ffmpeg 提取每条音轨或用 Audacity 导入。（S55, S56）
- 游戏解说（中文个人自媒体）：EV/QQ 录屏录画面 → CoolEdit 多轨录解说与配乐 → 剪映（按解说词生成字幕后改错字）或 PR（PS + TXT 批量生成字幕素材，再按配音拖动调整时长）→ 导出。（S57）
- AI 选段给游戏/播客的 clip：导入 → AI 选段与评分 → 逐条审阅 → 发布；游戏里 AI 不会"看"画面，所以漏掉无解说的高光。（S58, S64, S65）
- 带货直播切片：获达人授权 → 录制直播素材 → 手机剪辑 → 多账号发布 → 舍弃表现差的账号，爆款稍作改动后反复发布。（S59）
- 录播自动切片（B 站）：录制直播 → 渲染弹幕 → 语音转字幕 → 大模型选切片并生成标题 → 上传；运行在 VPS/Docker 上，与上传线路、API 配置打交道。（S61, S62, S63）
- Twitch/YouTube 链接 → 工具抓取并 AI 生成 clips → 按评分挑选 → 竖屏/字幕 → 发布。（S64, S65, S66）
- 混剪（B 端）：剪映"营销成片"或即创"一键成片"生成 → 无法调整 BGM/混剪逻辑/字体 → 放弃，回到传统方式批量混剪（作者称"才几个小时的工作量"）。（S68）
- 竖屏重构：Premiere 里把 16:9 序列用 Auto Reframe 转成 9:16 → 分析不完成或主体跑偏 → 取消勾选 scale to frame size、删掉效果后重新添加。（S69）
- 卡点：Shotcut 里把音乐重采样为整数拍/秒、项目用整数帧率 → 跳转距离设为一拍 → 逐拍跳转并加关键帧。（S70）
- CapCut Auto Cut：先出一个初稿 → 手动挪晚/早的切点、删重复片段、减少与节奏冲突的转场。（S71）
- 文档/脚本转视频：粘贴文本 → 选音色 → 自动匹配素材与字幕 → 人工浏览并替换不匹配素材、改字幕错字 → 导出（免费版带水印）。（S72, S73, S74, S75, S76）
- 鬼畜/YTPMV：选人声清晰的素材 → 调音、和声、混音 → 配画面字幕 → 卡点；在 Vegas 里用 MIDI 脚本把片段自动对拍，先把每个音频片段的 Pitch Change 调到 A4；音频质量要求高时转到 FL Studio；对拍要手动时靠整数帧率与逐拍关键帧。（S78, S79, S81, S70）

---

## 5. 爆款判断与复盘

本节按"平台官方说法 / 创作者或从业者经验帖 / 营销"区分来源性质；每条在末尾注明。没有找到带真实后台数据的创作者第一手复盘（Reddit、B 站、抖音、小红书不可达），因此以下以官方说法和经验帖为主。

- [S83] (quote) "Dips mean that viewers are abandoning or skipping at that specific part of your video." — YouTube 官方：留存曲线上的"掉点"；官方建议逐个排查原因（平台官方）
- [S83] (quote) "Spikes appear when more viewers are watching, rewatching, or sharing those parts of your video." — YouTube 官方；官方同时说尖峰也可能意味着"Your content isn't clear"，观众在重看（平台官方）
- [S83] (quote) "Intro tells you what percentage of your audience still watched your video after the first 30 seconds." — YouTube 官方的开头指标（平台官方）
- [S83] (paraphrase) 官方对开头的建议是改缩略图和标题以更贴合内容，并"Modify the first 30 seconds of your video and experiment with different styles"；页面没有给出 30 秒以外的剪辑节奏建议。 — 平台官方
- [S84] (paraphrase) 官方：平线表示观众从头看到尾；渐降表示观众逐渐失去兴趣，官方说随视频推进下降是正常的；可以和"typical retention"（最近 10 条类似时长的视频）比较；数据通常要 1–2 天才处理完。 — 平台官方
- [S84] (paraphrase) 官方：如果精彩片段出现得晚，应当把有吸引力的内容更早引入，因为观众规模通常随视频推进变小。 — 平台官方
- [S85] (paraphrase) Opus Clip 官方文档：每个切片有 0–99 的 virality score，由 Hook、Flow、Value、Trend 构成，"indicate a greater likelihood of attracting attention, engagement, and shares"；仅 Pro 和 Starter 计划可见，页面没有准确性免责声明。 — 厂商官方文档
- [S58] (quote) "as a ranking tool, not a guarantee" — 评测者对 virality score 的建议用法；评测者说批评者认为分数并不总能预测表现（评测者转述，非自测）
- [S64] (quote) "Ai highlights with rating scores help you pick the best ones." — 一位 Eklipse 用户用评分挑选 clip（用户评价）
- [S65] (paraphrase) 可见的 Opus Clip 评价里无人评论 virality score，只有一位 YouTube 用户称"has really helped us gain more followers and more views"。 — 用户评价，样本小
- [S66] (quote) "AI editing doesn't really work" — 一位 Vizard 用户的评价（用户评价）
- [S59] (paraphrase) 带货切片的从业方式是"爆款稍作改动后反复发布，直到流量消失"，表现差的账号被舍弃。 — 媒体报道，描述从业者做法
- [S60] (quote) "切片矩阵的确有效果，但能产生多大效果其实取决于主播本人。" — 切片公司员工对效果的判断（受访者）
- [S68] (paraphrase) 作者认为一键成片/营销成片这类 AI 成片"无法满足符合业务最低限度诉求"，需要额外人力修改。 — 经验帖（B 端产品从业者）
- [S77] (paraphrase) 创作者担心 AI/模板化批量视频被判定为重复内容而失去变现；YouTube 官方说这只是"minor update"。 — 媒体报道，含创作者与官方说法
- [S86] (quote) "黄金3秒法则至关重要，决定了观众是否愿意继续观看。" — 作者（有 4 年外贸营销经验）的断言，文中无数据（经验帖）
- [S86] (quote) "尽可能每隔5-10秒抛出一个爆点或者切换一个信息点，维持观众情绪。" — 同一作者对剪辑节奏的建议，是经验法则，没有测试结果（经验帖）
- [S87] (quote) "不是让它判断'好不好'，而是让它帮你拆'为什么'" — 自称上市公司 MCN 短视频负责人的作者谈用 AI 辅助复盘：播放量"只是结果，不是原因"（经验帖，示例是假设性的）
- [S87] (quote) "前5秒没有让用户知道为什么要继续看" — 同一作者认为开头不清楚往往是掉点原因（经验帖）
- [S87] (paraphrase) 作者主张的复盘 SOP 有五步：整理数据、解释数据、挖评论、改写标题和开头、建内容矩阵；评论区被当作"需求池"。 — 经验帖
- [S87] (paraphrase) 一位读者在评论里提出：数据驱动的复盘可能漏掉由情绪或偶然驱动的爆款。 — 读者评论
- [S88] (paraphrase) 厂商博客：早期每 10–15 秒在谈话、屏幕录制、特写、插入画面之间切换；节奏按受众年龄不同（13–24 岁 15–25 秒切一次，25 岁以上 20–40 秒）；过度剪辑（重缩放、whoosh、堆叠跳切）会让观众疲劳。 — 营销（文中多数节奏数字没有数据支撑）
- [S88] (quote) "A two-second gap with no movement or sound 'reads as the video being over.'" — 同一厂商博客谈死气（营销）
- [S88] (quote) "A sharp drop points at a specific moment in the edit, so scrub to that timestamp and watch the ten seconds before it." — 同一厂商博客的看图方法：到掉点的时间戳往前看十秒，查死气、无动机的切点或静态镜头，再重剪或加画面变化（营销）
- [S89] (paraphrase) 2022 年的自媒体推广文：认为完播率是平台推荐的重要因素之一，主张"把重点内容放在开头表达出来"；全文没有案例和数据。 — 营销
- [S27] (quote) "I feel like I'm listening to a 10 year old with ADHD when you cut out literally any pause whatsoever." — 对"全删停顿"的观众反应（HN 评论，见前文，属观众视角）
