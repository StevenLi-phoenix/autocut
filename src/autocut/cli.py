"""autocut 命令行：人和 AI agent 共用的入口。

输出契约（规格见 docs/agent-design/autocut/04-prototype/spec-cards.md「全局约定」）：
- stdout 是 TTY 且没加 --json：给人看的摘要（颜色、图标，遵守 NO_COLOR）；否则只输出**一个** JSON 对象。
- 成功 {"ok": true, "command", ..., "next": [{"cmd", "why"}], "notes": [...]}；
  失败 {"ok": false, "error": {"code", "message", "fix", "retry"}}。
- 退出码：0 成功 · 2 用法错误 · 3 输入错误 · 4 缺依赖 · 5 运行失败 · 1 意外错误。
- 进度和日志只写 stderr（默认 WARNING，-v INFO，-vv DEBUG；非 TTY 没有动画）；traceback 只在 --debug 时写 stderr。

给人和 agent 读的 help 文案全部集中在 HELP 里，方便按 eval 结果修改。
重模块（numpy、配方、渲染）在命令里才导入，`autocut --help` 保持秒开。
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO

from autocut import __version__
from autocut.plan import REASONS, UNCERTAIN_BELOW, Output, Plan, PlanError, Segment

log = logging.getLogger("autocut.cli")

# ================================================================== help 文案（集中在这里，按 eval 结果改）
#
# `$名字` 是从代码里取的值（build_parser 里用 string.Template 填）：$tight/$natural/$relaxed 来自 recipes/cut.py 的 PACES，
# $uncertain 来自 plan.UNCERTAIN_BELOW，$formats 来自 EXPORT_TARGETS。这样 help 和实现不会各说各的。

HELP: dict[str, str] = {
    # ---- 顶层
    "autocut": (
        "autocut：自动粗剪。先写一份可以审阅的方案（<素材名>.autocut.json），人或 agent 看过、改过，再出片或导出剪辑工程。\n"
        "\n"
        "选配方：口播/录课/游戏实况剪停顿和废话 → cut · 直播录像切几条短视频 → highlights · 素材 + BGM 卡点 → montage ·\n"
        "台词 + BGM 鬼畜 → ytpmv · 文稿 + 配图做配音视频 → narrate · 只要文字稿或字幕 → transcribe · 不确定 → analyze。\n"
        "只是转格式、改分辨率、截固定一段：直接用 ffmpeg，不需要 autocut。\n"
        "\n"
        "典型流程：autocut cut talk.mp4 → autocut show talk.autocut.json --filter uncertain → 让人核对（autocut review）→ autocut render\n"
        "方案以第一个素材命名（narrate 以文稿命名），路径以返回里的 plan 字段为准。缺什么先跑 autocut doctor，不要自己在磁盘上找模型。\n"
        "每个命令的详细说明：autocut <命令> --help"
    ),
    "autocut.epilog": (
        "全局约定（每个命令都一样）：\n"
        "  输出    stdout 是终端时打印给人看的摘要；不是终端（管道、agent）或加了 --json，只输出一个 JSON 对象。NO_COLOR 关闭颜色\n"
        '  成功    {"ok": true, "command": ..., "next": [{"cmd", "why"}], "notes": [...]}；next 是下一步可以直接复制的命令；\n'
        "          结果为空时带 notice（例如\"0 处拿不准：全部有把握\"）；tell_user 是可以原样转告用户的一句话\n"
        '  失败    {"ok": false, "error": {"code", "message", "fix", "retry"}}；不打印 traceback（--debug 时写到 stderr）\n'
        "  退出码  0 成功 · 2 用法错误（含 review 在非终端里没加 --detach）· 3 输入错误（文件不存在、方案不合法、会覆盖已有文件）·\n"
        "          4 缺依赖（ffmpeg、语音识别模型、say 的声音）· 5 运行失败（出片失败、超时、被 kill 取消）· 1 意外错误\n"
        "  日志    进度和日志只写 stderr：默认只写警告，-v 写过程，-vv 写调试信息；非终端时没有动画\n"
        "  改动    会写文件的命令都有 --dry-run；方案原子写入、revision +1；已有的方案、成片和工程都不覆盖，除非 --force\n"
        "  取消    长命令（render、transcribe）开始时在 stderr 写 pid=N；取消就 kill N（不要 pkill），写了一半的文件会被删掉\n"
        "  降级    不静默降级：缺语音识别模型时照样剪停顿，notes 写明原因和补法，summary.degraded=true\n"
        "  时间    一律是秒（浮点）；给人看的输出另附 mm:ss\n"
        "  长度    JSON 默认不超过约 4k token；列表默认 50 条，带 total、showing、next_offset\n"
        "不要直接读方案 JSON（几百个片段会占满上下文）：用 autocut show。改方案用 autocut set，不要手改。"
    ),
    "opt.json": "只输出一个 JSON 对象（stdout 不是终端时自动开启）",
    "opt.verbose": "在 stderr 写过程日志；-vv 写调试信息",
    "opt.debug": "出错时把 traceback 写到 stderr",
    "opt.out": "方案写到哪里（以 .json 结尾）；默认 <第一个素材名>.autocut.json，放在素材旁边。以返回里的 plan 字段为准",
    "opt.dry_run": "只打印摘要，不写方案",
    "opt.force": "目标方案已存在时也覆盖（会丢掉旧方案，包括人在审阅页里改过的地方）",
    "opt.lang": "识别语言：zh、en……（默认自动）",
    "opt.track": "用哪条音轨：音轨号（1 起）或标题，例如 --track 2、--track Mic（默认按标题猜人声轨）",
    # ---- doctor
    "doctor.short": "本机能力自检：ffmpeg、语音识别模型、say 的声音；缺什么、怎么补（不下载）",
    "doctor": (
        "本机能力自检：ffmpeg（以及字幕、文字、变调、响度、推拉这些滤镜）、whisper-cli 和语音识别模型、macOS `say` 的声音。"
        "缺什么就列出来，并给出具体的安装命令和预估下载量；只检查，**不会**下载或安装任何东西。\n"
        "需要转写就用 `autocut transcribe`，不要自己在磁盘上找模型，也不要用别的项目的环境。\n"
        "例子：`autocut doctor` → ffmpeg 9.0 ✓ · whisper-cli ✓ · 模型 large-v3-turbo ✓ · say：Tingting、Samantha ✓"
    ),
    # ---- transcribe（Description 原文来自 spec card）
    "transcribe.short": "逐词文字稿（谁在什么时候说了什么）：json / srt / txt，本机离线识别",
    "transcribe": (
        "本机离线语音识别（whisper.cpp），输出逐词文字稿：谁在什么时候说了什么。格式有 `json`（逐词时间戳）、`srt`、`txt`。"
        "需要知道素材里说了什么的时候用它，比如找重说、找高光里的台词、只要字幕；**不要自己在磁盘上找模型或别的项目里的 whisper**。"
        "没有模型时会直接告诉你缺什么、怎么补（也可以先跑 `autocut doctor`）。\n"
        "例子：`autocut transcribe talk.mp4 --format srt` → `talk.srt`，412 句，用的模型是 large-v3-turbo。\n"
        "需要转写就用它，不要自己在磁盘上找模型。不会自动下载任何东西。结果有缓存：之后 cut / highlights 不会再识别一遍。\n"
        "长任务：stderr 第一行是 pid=N，取消就 kill N。"
    ),
    "transcribe.media": "素材：视频或音频",
    "transcribe.format": "json（逐词时间戳，默认）| srt（字幕）| txt（纯文本，一句一行）",
    "transcribe.out": "写到哪里；默认素材旁边的 <素材名>.words.json / .srt / .txt",
    "transcribe.force": "覆盖已有文件",
    "transcribe.dry_run": "照常识别、打印摘要，但不写文件",
    # ---- transcript / clip（agent 自己选段：先读带句段 ID 的文字稿，再按 ID 指认起止）
    "transcript.short": "分页读带句段 ID 的文字稿（给 agent 选段用，不写文件）",
    "transcript": (
        "读素材的文字稿，一页一批，每句带句段 id（s0001…）和起始时间。给 agent 选段用：先用它读（`--around 1:05:00` 以某个时刻为中心取一页），"
        "再用 `autocut clip` 按句段 id 指认候选的起止。**时间由程序换算，不要自己写时间戳**。需要先有文字稿（第一次会识别，结果有缓存，之后很快）。\n"
        "例子：`autocut transcript talk.mp4 --limit 40` → 第 1–40 句（共 612 句），next_offset=40。"
    ),
    "transcript.media": "素材：视频或音频",
    "transcript.offset": "从第几句开始（默认 0）",
    "transcript.limit": "一页多少句（默认 40，最多 100）",
    "transcript.around": "以这个时刻为中心取一页（秒或 m:ss），忽略 --offset",
    "clip.short": "按句段 id 新建一条候选短片（程序吸附边界并做硬约束校验）",
    "clip": (
        "agent 自己选段：按 `autocut transcript` 给出的句段 id 指认起止（--from 起句、--to 止句，含两端），新建一条候选，写进方案。"
        "边界由程序吸附到句子边缘之外的停顿里，**任何一刀都不落在一句话中间**；硬约束不过就拒绝，方案不变，并告诉你为什么、怎么改："
        "时长不在 --length 内 · 起止切在话中间 · 开头 1 秒内没有人说话 · 结尾是半句 · 与已有候选重叠 · 落在 --forbid 区间或命中敏感词。\n"
        "第一个参数是素材（新建方案，默认 <素材名>.autocut.json）或已有的 clips 方案（继续添加）。候选默认不选中，`--select` 直接选中，之后用 `autocut set 方案 clip_1 --select`。"
        "出片用 `autocut render`；选段的好坏要独立评审，不要自己给自己打分。\n"
        "例子：`autocut clip talk.mp4 --from s0212 --to s0219 --title 为什么猫爱睡觉 --reason 问题直接抛出，自成一体` → clip_1，3:12–3:58（46s）。"
    ),
    "clip.target": "素材（新建方案）或已有的 clips 方案（.json）",
    "clip.from": "起句的句段 id，例如 s0212",
    "clip.to": "止句的句段 id（含），例如 s0219",
    "clip.title": "候选标题（≤60 字）",
    "clip.reason": "为什么选它（写进方案，给人看）",
    "clip.confidence": "把握度 0–1（默认 0.7）；低于 0.75 在方案里算拿不准",
    "clip.length": "时长范围 最短-最长 秒（默认 30-60）",
    "clip.forbid": "不可用区间（广告/赞助/被拒过的段）：开始-结束，可以重复",
    "clip.aspect": "成片画幅，默认 9:16；none 跟随素材",
    "clip.select": "直接选中这条候选",
    "clip.expect_revision": "方案的 revision 不是 N 就拒绝（防止覆盖别人的改动）",
    # ---- analyze
    "analyze.short": "不知道用哪个配方时，先看一眼素材：时长、音轨、语音占比、BPM → 建议",
    "analyze": (
        "看一眼素材，不剪：时长、画面（分辨率、帧率、是否可变帧率）、音轨（猜哪条是人声）、语音占比、BPM（纯音频时），"
        "并给出 `suggest`：适合哪个配方、为什么。只读素材，结果写进分析缓存，之后跑配方更快。\n"
        "不知道该用哪个配方时用它。\n"
        "例子：`autocut analyze talk.mp4` → 38:12 · 1920×1080 30fps VFR · 音轨 1 Mix、2 Mic（人声）· 62% 在说话 → 建议 `autocut cut talk.mp4`"
    ),
    "analyze.media": "素材：视频、音频、文稿（.md/.txt）或图片",
    # ---- cut（第一段是 spec card 的 Description 原文）
    "cut.short": "口播、录课、游戏实况：剪停顿、重说、口头禅 → 方案",
    "cut": (
        "给口播、录课、游戏实况做剪辑方案：剪掉长停顿、重说的段落和口头禅（\"嗯\"\"那个\"），保留说话。"
        "不出片，只写一份方案 `<素材名>.autocut.json`，打印摘要：剪掉多少、按理由分类计数、哪几刀拿不准。\n"
        "**适用**：用户说\"剪掉停顿/废话/说错的地方\"\"把这条录像剪干净\"。"
        "**不适用**：只想改个格式、截一段固定时间（直接用 ffmpeg）；直播找高光（用 `highlights`）；配乐卡点（用 `montage`）。\n"
        "有多条音轨时（OBS），自动挑标题像人声的那条来判断；猜不出来就用混音轨，并在 notes 里说明，也可以用 `--track` 指定。"
        "有语音识别模型时才能检测重说和口头禅（`autocut doctor` 可以查）。\n"
        "例子：`autocut cut talk.mp4` → `talk.autocut.json`，38:12 → 21:05，剪掉 23 处长停顿、14 处重说、9 处口头禅，3 处拿不准。\n"
        "\n"
        "把握度低于 $uncertain 的算拿不准。多个素材时方案以第一个素材命名，以返回里的 plan 字段为准；"
        "同名方案已经存在时不加 --force 不会覆盖（退出码 3，plan_exists）。"
    ),
    "cut.media": "素材路径，可以多个（按顺序接起来），例如 talk.mp4",
    "cut.pace": "每处停顿保留多长：tight ${tight}s | natural ${natural}s（默认）| relaxed ${relaxed}s（讲课）| 直接给秒数，例如 --pace 0.3",
    "cut.no_retakes": "不检测重说",
    "cut.no_fillers": "不检测口头禅",
    "cut.keep_action": "游戏实况里\"没人说话但游戏声很激烈\"的停顿是否保留：auto 表示素材有两条以上音轨时启用（默认 auto）",
    "cut.protect": "这些区间一律保留：开始-结束（秒或 m:ss，素材时间），可以重复，例如 --protect 61.5-75",
    # ---- highlights
    "highlights.short": "直播录像 → 几条候选高光短视频（9:16、字幕、识别抽奖刷屏）→ 方案",
    "highlights": (
        "直播或长录像切成几条短视频。理由看弹幕爆发、音量峰、关键词、画面剧变，**并识别抽奖刷屏这类诱饵**。"
        "和 `cut` 的区别：`cut` 输出一条剪短的长视频，`highlights` 输出多条独立短片。"
        "不出片，只写方案 `<素材名>.autocut.json`，打印候选清单：每条的时间、分数、理由；被排除的诱饵和排除原因；哪几条被选中（默认前 3 条）。\n"
        "已经包含：9:16 竖屏、烧录字幕、抽奖刷屏识别；--chat 可选。\n"
        "宁缺毋滥：真高光不够 n 条时不凑数，响度补位的弱候选只列出、不自动选，摘要写\"选中 k 条 / 要求 n 条\"；"
        "确认可用再 `autocut set PLAN clip_x --select`。分数只用来排序，不代表播放量。直播切片发布前要确认已获得主播授权。\n"
        "例子：`autocut highlights vod.mp4 --chat vod.chat.jsonl` → `vod.autocut.json`，8 个候选，选中 clip_1–clip_3，排除 1 条抽奖刷屏。"
    ),
    "highlights.vod": "直播录像或长视频（一次一个）",
    "highlights.chat": '可选：弹幕文件（JSONL，每行 {"t": 秒, "text": "内容"}，可以再带 "user"）；有它才能看弹幕爆发、识别抽奖刷屏',
    "highlights.n": "要几条（默认 3）；不够时不凑数",
    "highlights.candidates": "候选最多列几条，含没选中的、弱候选和诱饵（默认 n+5）",
    "highlights.length": "每条多长：最短-最长（秒），例如 15-30；只写一个数表示最长，最短取它的一半（默认 15-30）",
    "highlights.aspect": "成片画幅：9:16、16:9、1:1、4:5，或 source 跟随素材（默认 9:16）",
    "highlights.reframe": "改画幅时怎么裁：center 居中；track 跟着画面运动的重心（默认 center）",
    # ---- montage
    "montage.short": "多段素材 + BGM → 卡点混剪 → 方案",
    "montage": (
        "一堆素材配一首歌做卡点混剪：检测 BGM 的节拍，按拍或小节换镜头，每个镜头挑素材里运动强、和前一个镜头差别大的一段；"
        "素材原声静音，成片音频就是 BGM。不出片，只写方案 `<第一个素材名>.autocut.json`，打印摘要：BPM、小节数、镜头数、每个镜头来自哪段素材。\n"
        "**不适用**：口播剪停顿（用 `cut`）；台词对拍（用 `ytpmv`）。\n"
        "例子：`autocut montage clip*.mp4 --music bgm.wav --length 15` → `clip1.autocut.json`，120 BPM · 8 小节 · 16 个镜头。"
    ),
    "montage.clips": "视频素材，可以多个",
    "montage.music": "BGM（必填），节拍从它来",
    "montage.length": "目标时长（秒），吸附到整小节（默认 15）",
    "montage.cut_every": "多久换一个镜头：auto | beat | 2beats | bar（默认 auto）",
    "montage.aspect": "成片画幅，例如 9:16；默认跟随第一个素材",
    "montage.music_start": "从 BGM 的第几秒开始（吸附到小节首拍）；默认自动挑最响的一段",
    # ---- ytpmv
    "ytpmv.short": "台词 + BGM → 鬼畜材料包和第一版对拍 → 方案",
    "ytpmv": (
        "鬼畜：把台词切成音节、对到 BGM 的节拍上。输出是**材料**（音节切片、节拍网格）加第一版对拍，"
        "编排要靠人在剪辑软件里做（建议 `autocut export PLAN --format fcpxml`）。"
        "不出片，只写方案 `<素材名>.autocut.json`，打印摘要：音节数（附文字）、BPM、放置次数。\n"
        "例子：`autocut ytpmv line.mp4 --music bgm.wav` → `line.autocut.json`，12 个音节，120 BPM，4 小节 22 次放置。"
    ),
    "ytpmv.clip": "台词素材（视频或音频，一次一个）",
    "ytpmv.music": "BGM（必填），对拍的节奏从它来",
    "ytpmv.pattern": "放置节奏：auto | beat（每拍一个字）| half（每半拍一个字）（默认 auto）",
    "ytpmv.pitch": "按简单旋律给音节变调",
    "ytpmv.length": "目标时长（秒），吸附到整小节（默认 4 小节）",
    # ---- narrate
    "narrate.short": "文稿 + 配图 → 配音、字幕、配图的知识视频 → 方案",
    "narrate": (
        "按文稿生成配音、字幕、配图的知识视频：macOS `say` 逐句配音，按句子把配图分到时间轴上（每张至少停留 2.5s），"
        "开头的问题句做大字钩子，字幕烧录。不出片，只写方案 `<文稿名>.autocut.json`，打印摘要：配音时长、句数、每张图停留多久、开头钩子长度。\n"
        "工具不知道图里画了什么：要提醒人核对图和内容是否对应（文稿里写 `![说明](文件名)` 可以指定从哪句开始用哪张图）。\n"
        "例子：`autocut narrate script.md img1.png img2.png img3.png` → `script.autocut.json`，配音 58.2s · 14 句 · 3 张图。"
    ),
    "narrate.script": "文稿 .md / .txt（标题行和图片指令不朗读）",
    "narrate.images": "配图或视频片段，按顺序使用",
    "narrate.voice": "say 的声音名，例如 Tingting、Samantha；默认按文稿语言选（autocut doctor 看装了哪些）",
    "narrate.music": "可选 BGM（配音时自动压低 12dB）",
    "narrate.size": "成片尺寸：宽x高（默认 1920x1080）",
    "narrate.rate": "语速（词/分钟）；默认用 say 自己的语速",
    # ---- show（第一段是 spec card 的 Description 原文）
    "show.short": "读方案：摘要，或者分页列出片段（--filter uncertain 看拿不准的）",
    "show": (
        "读方案，不修改。默认打印摘要，和 `cut` 等命令的输出一样。加 `--segments` 分页列出片段；"
        "`--filter uncertain` 只看拿不准的，适合在交给人之前先自查。\n"
        "用来回答\"剪了什么、为什么剪\"。不要直接读整个方案 JSON，几百个片段会占满上下文。\n"
        "例子：`autocut show talk.autocut.json --filter uncertain` → 3 条，每条带时间、动作、理由、把握度和说了什么。\n"
        "\n"
        "把握度低于 $uncertain 的算拿不准。每个片段都带 start/end（素材时间）和 out_at（成片时间；剪掉的片段给它在成片里的接缝位置）。\n"
        "用户说\"3:12 那段\"通常是看成片说的：`--around 3:12` 默认按成片时间找，`--source-time` 改按素材时间。"
    ),
    "show.plan": "方案文件（*.autocut.json）",
    "show.output": "多条输出时（高光）看哪条；all 列出全部输出的摘要（默认：摘要看全部候选，片段看第一条）",
    "show.segments": "列片段，不只是摘要",
    "show.filter": "只列某类片段：all | uncertain | cut | keep | 理由名（silence、retake、filler……）；用了就隐含 --segments",
    "show.around": "只看这个时间点前后 ±10s 的片段：秒、m:ss 或 h:mm:ss；默认按成片时间；隐含 --segments",
    "show.source_time": "--around 按素材时间解释（默认按成片时间）",
    "show.limit": "每页几条（默认 50，最多 500）",
    "show.offset": "从第几条开始（默认 0）",
    # ---- set（第一段是 spec card 的 Description 原文）
    "set.short": "改方案：片段剪或留、挪边界、选或取消候选",
    "set": (
        "修改方案，和审阅页里的操作等价：`--keep`/`--cut` 切换片段的剪或留；`--start`/`--end` 挪边界，首尾相接的邻居会跟着动；"
        "`--select`/`--deselect` 选或取消候选输出（高光）。改动会写进方案，revision 加 1，被改的片段标记为人工确认（confidence 1.0）。\n"
        "先用 `show` 找到片段 id，再用 `set`。不要手改 JSON：`set` 会校验不变量（边界不越过邻居、不出素材范围），手改容易把方案改坏。\n"
        "例子：`autocut set talk.autocut.json d031 d044 --keep` → 2 处改为保留，成片 +1.3s。"
    ),
    "set.plan": "方案文件",
    "set.ids": "片段 id（d031）或输出 id（clip_2），可以多个",
    "set.keep": "改成保留",
    "set.cut": "改成剪掉",
    "set.start": "把片段起点挪到这里（素材时间，秒或 m:ss）；首尾相接的邻居跟着动；只能对一个片段用",
    "set.end": "把片段终点挪到这里（素材时间，秒或 m:ss）；首尾相接的邻居跟着动；只能对一个片段用",
    "set.select": "选中候选输出（高光的 clip_x），render 会出它",
    "set.deselect": "取消选中候选输出",
    "set.output": "片段 id 在多条输出里重复时（高光的 c001……），指定哪条输出",
    "set.expect_revision": "方案的 revision 不是 N 就拒绝修改（防止覆盖人在审阅页里的改动）",
    "set.dry_run": "只显示会怎么变，不保存",
    # ---- review
    "review.short": "本地审阅页让人核对（会一直运行；agent 把命令交给用户，或加 --detach 后台打开）",
    "review": (
        "打开本地审阅页（人用）：浏览器里看文字稿和时间线，逐条切换剪或留、拖边界、选候选，改动直接存回方案（revision +1）。"
        "服务只绑定 127.0.0.1。\n"
        "在终端里：前台运行到 Ctrl+C，退出时打印 revision 的变化。\n"
        "不是终端时（agent 调用）**必须加 `--detach`**：在后台启动（独立进程组），立即返回 {url, pid, stop}；"
        "不加就立即失败（退出码 2，would_block），免得把调用方卡住。\n"
        "agent 一般把 `autocut review PLAN` 交给用户自己运行；要替用户打开才用 --detach，用完 kill <pid>。"
        "自己检查方案用 `autocut show PLAN --filter uncertain`。\n"
        "例子：`autocut review talk.autocut.json --detach` → {\"url\": \"http://127.0.0.1:52011/\", \"pid\": 4242, \"stop\": \"kill 4242\"}"
    ),
    "review.plan": "方案文件",
    "review.detach": "后台启动并立即返回网址和 pid（不是终端时必须加）；关掉用 kill <pid>",
    "review.port": "端口（默认 0：系统挑一个空闲的）",
    "review.no_open": "不自动打开浏览器",
    "review.dry_run": "只检查方案能不能打开，不启动服务",
    # ---- render（第一段是 spec card 的 Description 原文）
    "render.short": "按方案出片，出片后独立核验（默认不覆盖已有文件）",
    "render": (
        "按方案出片（ffmpeg）。默认输出到方案旁边：`<名>.<输出id>.mp4`；方案开启字幕时同时写 `.srt`。高光会出所有被选中的输出。"
        "完成后核验时长和音画同步，结果写进 JSON。\n"
        "出片前最好先让人看过方案（`review`）或者自己 `show --filter uncertain`。只想交给剪辑软件时用 `export`，不要出片。\n"
        "例子：`autocut render talk.autocut.json` → `talk.main.mp4`，21:05，音画漂移 0.00s，-14.1 LUFS。\n"
        "\n"
        "核验（独立于出片过程，结果在每条的 verify 里）：时长、音画漂移、响度（开了响度标准化时）、"
        "接缝是否落在说话中间（在成片上重新检测语音）。核验不过写进 warnings，ok 仍为 true、verify.verified=false。\n"
        "长任务：stderr 第一行是 pid=N，取消就 kill N（不要 pkill）；被取消时会停掉 ffmpeg、删掉写了一半的文件，退出码 5（cancelled）。"
    ),
    "render.plan": "方案文件",
    "render.output": "只出这条输出，可以重复；默认出所有被选中的",
    "render.out_dir": "成片写到哪个目录（默认方案旁边）",
    "render.burn": "烧录字幕 / 不烧录（默认听方案的）",
    "render.loudnorm": "响度标准化的目标 LUFS（例如 -14），或 off；默认竖屏 -14、横屏不做",
    "render.force": "覆盖已有的成片",
    "render.no_verify": "跳过出片后的核验",
    "render.dry_run": "只打印 ffmpeg 命令和滤镜图的位置，不出片",
    # ---- export（第一段是 spec card 的 Description 原文）
    "export.short": "导出剪辑软件工程或字幕：fcpxml、xmeml、edl、srt、markers",
    "export": (
        "把方案导出给剪辑软件或字幕平台：`fcpxml`（Final Cut、DaVinci Resolve）、`xmeml`（Premiere）、`edl`、`srt`、"
        "`markers`（只打标记，不剪，适合想自己剪的人）。被剪掉的段落默认写成标记（`--cuts markers`），在剪辑软件里还看得见；"
        "也可以写成禁用片段（`disabled`），或者干脆不写（`none`）。\n"
        "例子：`autocut export talk.autocut.json --format fcpxml` → `talk.main.fcpxml`\n"
        "\n"
        "格式和软件：$formats。\n"
        "导出后会做结构自检，但没有在剪辑软件里打开过：返回 verified_in_nle=false；给用户转述用返回里的 tell_user，不要说\"能导入\"。"
    ),
    "export.plan": "方案文件",
    "export.format": "$formats",
    "export.output": "导出哪条输出（默认第一条）",
    "export.cuts": "被剪掉的段落怎么写进工程（只对 fcpxml 有效）：markers 标记（默认）| disabled 禁用片段 | none 不写",
    "export.out": "写到哪里；默认方案旁边的 <名>.<输出id>.<扩展名>",
    "export.force": "覆盖已有文件",
    "export.dry_run": "只显示会写到哪里，不写",
    # ---- check（Description 原文来自 spec card）
    "check.short": "发布前体检：留存风险信号，每项附时间点和修法（不是爆款保证）",
    "check": (
        "发布前体检：找出会让观众划走的地方，每一项都给时间点和修法。检查项包括开头多久进正题、死寂、节奏、响度、画幅和平台是否匹配、时长、字幕。"
        "对成片（mp4）和方案（json）都能做；对方案做的话不用出片，几秒钟就有结果。\n"
        "**这是留存风险信号，不是爆款保证。**向用户转述时要说清楚这一点。\n"
        "例子：`autocut check talk.main.mp4 --platform bilibili` → 2 项风险：\"0:00–0:12 开头 12 秒才进入正题……\""
    ),
    "check.target": "成片（mp4……）或方案（.json）；对方案做不用出片，几秒出结果",
    "check.platform": "目标平台：auto（按画幅猜）| douyin | tiktok | shorts | reels | bilibili | youtube（默认 auto）",
    "check.output": "方案有多条输出时体检哪条（默认第一条）",
    "check.no_asr": "不跑语音识别（更快；没有现成文字稿时只做声学检查）",
    "check.save": "把报告存成 JSON（发布后用来对照留存曲线）",
}

# 导出格式 → 剪辑软件（help 和 tell_user 共用；键和 export.FORMATS 一致，测试保证）
EXPORT_TARGETS: dict[str, str] = {
    "fcpxml": "Final Cut Pro、DaVinci Resolve",
    "xmeml": "Premiere Pro",
    "edl": "通用格式（几乎所有剪辑软件都能读，只有主轨剪辑点）",
    "srt": "字幕（平台上传或剪辑软件的字幕轨）",
    "markers": "Final Cut Pro（只打标记、不剪）",
}


def _help(key: str) -> str:
    """HELP[key]，把 $名字 换成代码里的真实取值。"""
    from autocut.recipes.cut import PACES as CUT_PACES

    values = {k: f"{v:g}" for k, v in CUT_PACES.items()}
    values["uncertain"] = f"{UNCERTAIN_BELOW:g}"
    values["formats"] = " | ".join(f"{k}（{v}）" for k, v in EXPORT_TARGETS.items())
    return string.Template(HELP[key]).safe_substitute(values)


# ================================================================== 常量

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INPUT, EXIT_DEPENDENCY, EXIT_RUNTIME = 0, 1, 2, 3, 4, 5
EXIT_INTERRUPTED = 130

LIST_LIMIT = 50  # 列表默认条数
LIST_LIMIT_MAX = 500
UNCERTAIN_LIMIT = 10  # 摘要里 uncertain 最多列几条
CANDIDATE_LIMIT = 20  # 高光候选最多列几条
CHECK_ITEMS_LIMIT = 20  # 体检项最多列几条
PREVIEW_LIMIT = 20  # transcribe 的 preview 最多几句
TEXT_MAX = 60  # 片段文字稿截断长度
DETAIL_MAX = 90
AROUND = 10.0  # show --around 的半径（秒）
JOINT_MARGIN = 0.06  # 接缝离语音区间两端都超过这么多秒，才算"落在说话中间"
LOUDNESS_TOL = 1.5  # 成片实测响度和目标差这么多 LU 以内算达标
SHOW_FILTERS = ("all", "uncertain", "cut", "keep", *REASONS)
WEAK_MARK = "响度补位"  # highlights 给弱候选写的理由前缀（recipes/highlights.py）
BAIT_MARK = "刷屏"  # highlights 给诱饵写的理由里一定有这个词

MEDIA_HINT = "支持 mp4/mov/mkv/webm/wav/m4a/mp3…"
DEFAULT_VOICES = ("Tingting", "Samantha")  # narrate 按文稿语言默认用的声音（中文 / 英文）
SEVERITY_ORDER = {"risk": 0, "warn": 1, "info": 2}

# doctor：render 用到的 ffmpeg 滤镜 → 缺了会影响什么
FFMPEG_FILTERS = {
    "ass": "烧录字幕",
    "drawtext": "文字叠加（知识视频的开头钩子）",
    "rubberband": "鬼畜变调（ytpmv --pitch）",
    "loudnorm": "响度标准化",
    "zoompan": "图片缓慢推拉（知识视频）",
}
WHISPER_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
# review --detach 的后台进程：直接调用 review.server.serve（URL 写在它的 stderr 里）
_REVIEW_CHILD = "import sys; from autocut.review.server import serve; serve(sys.argv[1], port=int(sys.argv[2]), open_browser=sys.argv[3] == '1')"


# ================================================================== 错误


class CliError(Exception):
    """命令失败：带错误码、给人和 agent 看的原因与修法、退出码。"""

    def __init__(self, code: str, message: str, fix: str, exit_code: int = EXIT_INPUT, *, retry: bool = False) -> None:
        super().__init__(message)
        self.code, self.message, self.fix, self.exit_code, self.retry = code, message, fix, exit_code, retry

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "fix": self.fix, "retry": self.retry}


class _Cancelled(Exception):
    """收到 SIGTERM：子进程已经停掉，调用方负责删掉写了一半的文件。"""


class _UsageError(Exception):
    def __init__(self, message: str, parser: argparse.ArgumentParser) -> None:
        super().__init__(message)
        self.parser = parser

    def to_cli(self) -> CliError:
        usage = " ".join(self.parser.format_usage().split()).removeprefix("usage: ")
        return CliError("usage", f"参数不对：{self}", f"看 `{self.parser.prog} --help`。用法：{usage}", EXIT_USAGE)


def _classify(e: BaseException, command: str) -> CliError:
    """把各模块的异常归到退出码：输入错误 3、缺依赖 4、运行失败 5、意外错误 1。"""
    if isinstance(e, CliError):
        return e
    if isinstance(e, _Cancelled):
        return CliError("cancelled", f"收到 SIGTERM，{command} 已取消：停掉了它启动的子进程，删掉了写了一半的文件",
                        "要继续就重新运行同一条命令", EXIT_RUNTIME, retry=True)
    if isinstance(e, KeyboardInterrupt):
        return CliError("interrupted", "被 Ctrl+C 中断了", "重新运行同一条命令即可：方案和成片都是原子写入，不会留下写了一半的文件",
                        EXIT_INTERRUPTED, retry=True)
    if isinstance(e, PlanError):
        return CliError("plan_invalid", str(e), "如果方案是手改坏的，用配方重新生成；改方案请用 autocut set（会校验）", EXIT_INPUT)
    from autocut.probe import MediaError
    from autocut.recipes._common import RecipeError
    from autocut.render import RenderError
    from autocut.transcribe import TranscribeError

    msg = str(e)
    if isinstance(e, (MediaError, RenderError)) and ("找不到 ffmpeg" in msg or "找不到 ffprobe" in msg):
        return CliError("missing_ffmpeg", msg, "brew install ffmpeg，然后 autocut doctor", EXIT_DEPENDENCY)
    if isinstance(e, RecipeError):
        if "`say`" in msg:
            return CliError("missing_say", msg, "narrate 只能在 macOS 上运行（用系统的 say 配音）", EXIT_DEPENDENCY)
        return CliError("recipe_input", msg, f"按上面的说明改参数或换素材后重跑；参数说明见 autocut {command} --help", EXIT_INPUT)
    if isinstance(e, MediaError):
        return CliError("bad_media", msg, f"确认文件是完整的音视频（ffprobe 能打开）；{MEDIA_HINT}", EXIT_INPUT)
    if isinstance(e, FileNotFoundError):
        return CliError("not_found", msg, "检查路径（相对路径以当前目录为准；方案里的素材路径以方案所在目录为准）", EXIT_INPUT)
    if isinstance(e, RenderError):
        return CliError("render_failed", msg, "加 --dry-run 看 ffmpeg 命令和滤镜图文件，-vv 看详细日志", EXIT_RUNTIME)
    if isinstance(e, TranscribeError):
        return CliError("asr_failed", msg, "autocut doctor 检查语音识别；或者先加 --no-retakes --no-fillers 只剪停顿", EXIT_RUNTIME)
    if isinstance(e, subprocess.TimeoutExpired):
        return CliError("timeout", f"子进程超时：{e}", "稍后重试；素材很长时先用短一点的片段试", EXIT_RUNTIME, retry=True)
    if isinstance(e, OSError):
        return CliError("os_error", f"{type(e).__name__}: {e}", "检查文件权限、磁盘空间和端口占用", EXIT_RUNTIME)
    return CliError("internal", f"意外错误：{type(e).__name__}: {e}",
                    "加 --debug 重跑看 traceback，把命令和输出一起报告给开发者", EXIT_ERROR)


# ================================================================== 终端样式与进度

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _color_ok(stream: TextIO) -> bool:
    return _isatty(stream) and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


class Style:
    _CODES = {"ok": "32", "warn": "33", "err": "31", "cut": "35", "dim": "2", "bold": "1", "accent": "36"}

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, kind: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\x1b[{self._CODES[kind]}m{text}\x1b[0m"


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in _ANSI.sub("", s))


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _width(s))


class Activity:
    """stderr 上的一行动画：TTY 时显示 spinner 或进度条；非 TTY 时什么都不画（过程只在 -v 的日志里）。"""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, enabled: bool, style: Style) -> None:
        self.enabled, self.style = enabled, style
        self._lock = threading.Lock()
        self._drawn = False
        self._label = ""
        self._frac: float | None = None
        self._t0 = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @contextlib.contextmanager
    def spin(self, label: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        self._label, self._frac, self._t0 = label, None, time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autocut-activity", daemon=True)
        self._thread.start()
        try:
            yield
        finally:
            self._stop.set()
            self._thread.join()
            self._thread = None
            self.clear()

    def update(self, label: str, frac: float | None = None) -> None:
        self._label, self._frac = label, frac

    def _loop(self) -> None:
        i = 0
        while not self._stop.wait(0.1):
            self._draw(i)
            i += 1

    def _draw(self, i: int) -> None:
        el = time.monotonic() - self._t0
        if self._frac is None:
            line = f"{self.FRAMES[i % len(self.FRAMES)]} {self._label}  {el:.0f}s"
        else:
            n = int(max(0.0, min(1.0, self._frac)) * 24)
            line = f"{self._label}  {'█' * n}{'░' * (24 - n)} {self._frac:4.0%}  {el:.0f}s"
        with self._lock:
            sys.stderr.write("\r\x1b[2K" + self.style("accent", line))
            sys.stderr.flush()
            self._drawn = True

    def clear(self) -> None:
        with self._lock:
            if self._drawn:
                sys.stderr.write("\r\x1b[2K")
                sys.stderr.flush()
                self._drawn = False


class _TtyFormatter(logging.Formatter):
    def __init__(self, style: Style) -> None:
        super().__init__()
        self.style = style

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            return self.style("err", f"✗ {msg}")
        if record.levelno >= logging.WARNING:
            return self.style("warn", f"⚠ {msg}")
        if record.levelno >= logging.INFO:
            return self.style("dim", f"· {msg}")
        return self.style("dim", f"  {record.name}: {msg}")


class _LogHandler(logging.StreamHandler):
    """写 stderr；写之前先擦掉动画那一行，免得日志和进度条搅在一起。"""

    def __init__(self, activity: Activity) -> None:
        super().__init__(sys.stderr)
        self.activity = activity

    def emit(self, record: logging.LogRecord) -> None:
        self.activity.clear()
        super().emit(record)


@contextlib.contextmanager
def _logging_to_stderr(verbose: int, activity: Activity, style: Style, tty: bool) -> Iterator[None]:
    """autocut 的日志写 stderr；结束时恢复原样（在测试或别的程序里调用 main() 时不留下 handler）。"""
    root = logging.getLogger("autocut")
    old_level = root.level
    handler = _LogHandler(activity)
    handler.setFormatter(_TtyFormatter(style) if tty else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose >= 2 else logging.INFO if verbose == 1 else logging.WARNING)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


@dataclass
class Ui:
    json: bool
    style: Style  # stdout 用
    err_style: Style  # stderr 用
    activity: Activity
    argv: list[str]


# ================================================================== 小工具


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(float(x), n)


def _mmss(t: float | None, tenths: bool = False) -> str:
    if t is None:
        return "—"
    t = max(0.0, float(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    sec = f"{s:04.1f}" if tenths else f"{int(s):02d}"
    return f"{int(h)}:{int(m):02d}:{sec}" if h >= 1 else f"{int(m):02d}:{sec}"


def _rel(p: str | Path | None) -> str | None:
    """路径显示：在当前目录之下用相对路径（短、可以直接复制），否则用绝对路径。"""
    if p is None:
        return None
    path = Path(p)
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _q(p: str | Path | None) -> str:
    return shlex.quote(str(p))


def _trunc(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _first_line(e: BaseException) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__


def _rerun_without(argv: Sequence[str], flag: str) -> str:
    return "autocut " + shlex.join([a for a in argv if a != flag])


def _nx(cmd: str, why: str) -> dict[str, str]:
    return {"cmd": cmd, "why": why}


# ---- argparse 的取值解析（失败 → 用法错误，退出码 2）

_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d*)?)$")


def parse_time(s: str) -> float:
    """秒（"61.5"）或 [h:]mm:ss(.f)（"1:01.5"）→ 秒。"""
    v = s.strip()
    m = _TIME_RE.match(v)
    try:
        t = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} 不是时间：写秒数（61.5）或 mm:ss（1:01.5）") from None
    if t < 0:
        raise argparse.ArgumentTypeError(f"{s!r}：时间不能为负")
    return t


def parse_range(s: str) -> tuple[float, float]:
    """"61.5-75" / "1:01.5-1:15" → (61.5, 75.0)。"""
    m = re.match(r"^\s*([^-–~]+?)\s*[-–~]\s*([^-–~]+?)\s*$", s)
    if not m:
        raise argparse.ArgumentTypeError(f"{s!r} 看不懂：写成 开始-结束，例如 61.5-75 或 1:01.5-1:15")
    a, b = parse_time(m.group(1)), parse_time(m.group(2))
    if not a < b:
        raise argparse.ArgumentTypeError(f"{s!r}：开始要早于结束")
    return a, b


def parse_pace(s: str) -> str | float:
    from autocut.recipes.cut import PACES

    v = s.strip().lower()
    if v in PACES:
        return v
    try:
        x = float(v)
    except ValueError:
        named = "、".join(f"{k}（每处停顿留 {sec:g}s）" for k, sec in PACES.items())
        raise argparse.ArgumentTypeError(f"{s!r} 看不懂：用 {named}，或直接给秒数，例如 --pace 0.3") from None
    if not 0 <= x <= 10:
        raise argparse.ArgumentTypeError(f"{s!r}：每处停顿保留的秒数要在 0–10 之间")
    return x


def parse_length_range(s: str) -> tuple[float, float]:
    """"15-30" → (15, 30)；只写一个数 "30" → (15, 30)（最长 30，最短取一半）。"""
    try:
        x = float(s)
    except ValueError:
        return parse_range(s)
    if x <= 0:
        raise argparse.ArgumentTypeError(f"{s!r}：时长要大于 0")
    return x / 2, x


def parse_size(s: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\s*[x×X*]\s*(\d+)\s*$", s)
    if not m or int(m.group(1)) < 16 or int(m.group(2)) < 16:
        raise argparse.ArgumentTypeError(f"{s!r} 看不懂：写成 宽x高，例如 1920x1080 或 1080x1920")
    return int(m.group(1)), int(m.group(2))


def parse_loudnorm(s: str) -> bool | float | None:
    v = s.strip().lower()
    if v in ("off", "none", "no", "false"):
        return False
    if v in ("on", "true"):
        return True
    if v == "auto":
        return None
    try:
        x = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} 看不懂：写目标 LUFS（例如 -14）、off 或 auto") from None
    if not -40 <= x <= -5:
        raise argparse.ArgumentTypeError(f"{s!r}：目标响度应在 -40 到 -5 LUFS 之间（短视频平台常用 -14）")
    return x


def positive_int(s: str) -> int:
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} 不是整数") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"{s!r}：至少是 1")
    return n


def nonneg_int(s: str) -> int:
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} 不是整数") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"{s!r}：不能为负")
    return n


# ================================================================== 解析器


class _Parser(argparse.ArgumentParser):
    """用法错误不直接退出：交给 main 按输出契约打印（JSON 或给人看的），退出码 2。"""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message, self)


class _Fmt(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=30, width=min(110, shutil.get_terminal_size((100, 24)).columns))


def build_parser() -> _Parser:
    # 取值列表从各模块取（和实现保持一致）；这些模块会带进 numpy，--help 大约多 0.1s
    from autocut.check import PLATFORMS
    from autocut.export import CUT_MODES, FORMATS
    from autocut.recipes.montage import CUT_EVERY
    from autocut.recipes.ytpmv import PATTERNS

    common = _Parser(add_help=False)
    g = common.add_argument_group("通用选项")
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=_help("opt.json"))
    g.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS, help=_help("opt.verbose"))
    g.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help=_help("opt.debug"))

    p = _Parser(prog="autocut", description=_help("autocut"), epilog=_help("autocut.epilog"), formatter_class=_Fmt,
                parents=[common], suggest_on_error=True)
    p.add_argument("--version", action="version", version=f"autocut {__version__}")
    sub = p.add_subparsers(dest="command", title="命令", metavar="<命令>", required=True)

    def add(name: str) -> _Parser:
        return sub.add_parser(name, help=_help(f"{name}.short"), description=_help(name), formatter_class=_Fmt,
                              parents=[common], suggest_on_error=True)

    def plan_writer(sp: _Parser) -> None:
        sp.add_argument("-o", "--out", metavar="PATH", help=_help("opt.out"))
        sp.add_argument("--dry-run", action="store_true", help=_help("opt.dry_run"))
        sp.add_argument("--force", action="store_true", help=_help("opt.force"))

    add("doctor")

    sp = add("transcribe")
    sp.add_argument("media", metavar="MEDIA", help=_help("transcribe.media"))
    sp.add_argument("--lang", metavar="LANG", help=_help("opt.lang"))
    sp.add_argument("--track", metavar="N|TITLE", help=_help("opt.track"))
    sp.add_argument("--format", choices=("json", "srt", "txt"), default="json", help=_help("transcribe.format"))
    sp.add_argument("-o", "--out", metavar="PATH", help=_help("transcribe.out"))
    sp.add_argument("--force", action="store_true", help=_help("transcribe.force"))
    sp.add_argument("--dry-run", action="store_true", help=_help("transcribe.dry_run"))

    sp = add("transcript")
    sp.add_argument("media", metavar="MEDIA", help=_help("transcript.media"))
    sp.add_argument("--offset", type=int, default=0, metavar="N", help=_help("transcript.offset"))
    sp.add_argument("--limit", type=int, default=40, metavar="N", help=_help("transcript.limit"))
    sp.add_argument("--around", type=parse_time, metavar="T", help=_help("transcript.around"))
    sp.add_argument("--lang", metavar="LANG", help=_help("opt.lang"))

    sp = add("clip")
    sp.add_argument("target", metavar="MEDIA|PLAN", help=_help("clip.target"))
    sp.add_argument("--from", dest="first", required=True, metavar="ID", help=_help("clip.from"))
    sp.add_argument("--to", dest="last", required=True, metavar="ID", help=_help("clip.to"))
    sp.add_argument("--title", required=True, help=_help("clip.title"))
    sp.add_argument("--reason", required=True, help=_help("clip.reason"))
    sp.add_argument("--confidence", type=float, default=0.7, metavar="0-1", help=_help("clip.confidence"))
    sp.add_argument("--length", type=parse_range, default=(30.0, 60.0), metavar="最短-最长", help=_help("clip.length"))
    sp.add_argument("--forbid", type=parse_range, action="append", default=[], metavar="START-END", help=_help("clip.forbid"))
    sp.add_argument("--aspect", default="9:16", metavar="W:H|none", help=_help("clip.aspect"))
    sp.add_argument("--select", action="store_true", help=_help("clip.select"))
    sp.add_argument("--lang", metavar="LANG", help=_help("opt.lang"))
    sp.add_argument("--expect-revision", type=int, metavar="N", help=_help("clip.expect_revision"))
    plan_writer(sp)

    sp = add("analyze")
    sp.add_argument("media", metavar="MEDIA", help=_help("analyze.media"))

    sp = add("cut")
    sp.add_argument("media", metavar="MEDIA", nargs="+", help=_help("cut.media"))
    sp.add_argument("--pace", type=parse_pace, default="natural", metavar="tight|natural|relaxed|秒", help=_help("cut.pace"))
    sp.add_argument("--no-retakes", action="store_true", help=_help("cut.no_retakes"))
    sp.add_argument("--no-fillers", action="store_true", help=_help("cut.no_fillers"))
    sp.add_argument("--lang", metavar="LANG", help=_help("opt.lang"))
    sp.add_argument("--track", metavar="N|TITLE", help=_help("opt.track"))
    sp.add_argument("--keep-action", choices=("auto", "on", "off"), default="auto", help=_help("cut.keep_action"))
    sp.add_argument("--protect", type=parse_range, action="append", default=[], metavar="START-END", help=_help("cut.protect"))
    plan_writer(sp)

    sp = add("highlights")
    sp.add_argument("vod", metavar="VOD", help=_help("highlights.vod"))
    sp.add_argument("--chat", metavar="CHAT.jsonl", help=_help("highlights.chat"))
    sp.add_argument("-n", type=positive_int, default=3, metavar="N", help=_help("highlights.n"))
    sp.add_argument("--candidates", type=positive_int, metavar="N", help=_help("highlights.candidates"))
    sp.add_argument("--length", type=parse_length_range, default=(15.0, 30.0), metavar="MIN-MAX", help=_help("highlights.length"))
    sp.add_argument("--aspect", default="9:16", metavar="W:H|source", help=_help("highlights.aspect"))
    sp.add_argument("--reframe", choices=("center", "track"), default="center", help=_help("highlights.reframe"))
    sp.add_argument("--lang", metavar="LANG", help=_help("opt.lang"))
    plan_writer(sp)

    sp = add("montage")
    sp.add_argument("clips", metavar="CLIP", nargs="+", help=_help("montage.clips"))
    sp.add_argument("--music", required=True, metavar="BGM", help=_help("montage.music"))
    sp.add_argument("--length", type=float, default=15.0, metavar="秒", help=_help("montage.length"))
    sp.add_argument("--cut-every", choices=CUT_EVERY, default="auto", help=_help("montage.cut_every"))
    sp.add_argument("--aspect", metavar="W:H", help=_help("montage.aspect"))
    sp.add_argument("--music-start", type=parse_time, metavar="秒", help=_help("montage.music_start"))
    plan_writer(sp)

    sp = add("ytpmv")
    sp.add_argument("clip", metavar="CLIP", help=_help("ytpmv.clip"))
    sp.add_argument("--music", required=True, metavar="BGM", help=_help("ytpmv.music"))
    sp.add_argument("--pattern", choices=PATTERNS, default="auto", help=_help("ytpmv.pattern"))
    sp.add_argument("--pitch", action="store_true", help=_help("ytpmv.pitch"))
    sp.add_argument("--length", type=float, metavar="秒", help=_help("ytpmv.length"))
    sp.add_argument("--lang", metavar="LANG", help=_help("opt.lang"))
    plan_writer(sp)

    sp = add("narrate")
    sp.add_argument("script", metavar="SCRIPT", help=_help("narrate.script"))
    sp.add_argument("images", metavar="IMAGE", nargs="+", help=_help("narrate.images"))
    sp.add_argument("--voice", metavar="NAME", help=_help("narrate.voice"))
    sp.add_argument("--music", metavar="BGM", help=_help("narrate.music"))
    sp.add_argument("--size", type=parse_size, default=(1920, 1080), metavar="WxH", help=_help("narrate.size"))
    sp.add_argument("--rate", type=positive_int, metavar="WPM", help=_help("narrate.rate"))
    plan_writer(sp)

    sp = add("show")
    sp.add_argument("plan", metavar="PLAN", help=_help("show.plan"))
    sp.add_argument("--output", metavar="ID|all", help=_help("show.output"))
    sp.add_argument("--segments", action="store_true", help=_help("show.segments"))
    sp.add_argument("--filter", choices=SHOW_FILTERS, metavar="FILTER", help=_help("show.filter"))
    sp.add_argument("--around", type=parse_time, metavar="T", help=_help("show.around"))
    sp.add_argument("--source-time", action="store_true", help=_help("show.source_time"))
    sp.add_argument("--limit", type=positive_int, default=LIST_LIMIT, metavar="N", help=_help("show.limit"))
    sp.add_argument("--offset", type=nonneg_int, default=0, metavar="N", help=_help("show.offset"))

    sp = add("set")
    sp.add_argument("plan", metavar="PLAN", help=_help("set.plan"))
    sp.add_argument("ids", metavar="ID", nargs="+", help=_help("set.ids"))
    ops = sp.add_mutually_exclusive_group()
    ops.add_argument("--keep", dest="op", action="store_const", const="keep", help=_help("set.keep"))
    ops.add_argument("--cut", dest="op", action="store_const", const="cut", help=_help("set.cut"))
    ops.add_argument("--select", dest="op", action="store_const", const="select", help=_help("set.select"))
    ops.add_argument("--deselect", dest="op", action="store_const", const="deselect", help=_help("set.deselect"))
    sp.add_argument("--start", type=parse_time, metavar="T", help=_help("set.start"))
    sp.add_argument("--end", type=parse_time, metavar="T", help=_help("set.end"))
    sp.add_argument("--output", metavar="ID", help=_help("set.output"))
    sp.add_argument("--expect-revision", type=nonneg_int, metavar="N", help=_help("set.expect_revision"))
    sp.add_argument("--dry-run", action="store_true", help=_help("set.dry_run"))

    sp = add("review")
    sp.add_argument("plan", metavar="PLAN", help=_help("review.plan"))
    sp.add_argument("--detach", action="store_true", help=_help("review.detach"))
    sp.add_argument("--port", type=nonneg_int, default=0, metavar="N", help=_help("review.port"))
    sp.add_argument("--no-open", action="store_true", help=_help("review.no_open"))
    sp.add_argument("--dry-run", action="store_true", help=_help("review.dry_run"))

    sp = add("render")
    sp.add_argument("plan", metavar="PLAN", help=_help("render.plan"))
    sp.add_argument("--output", action="append", metavar="ID", help=_help("render.output"))
    sp.add_argument("--out-dir", metavar="DIR", help=_help("render.out_dir"))
    sp.add_argument("--burn-captions", action=argparse.BooleanOptionalAction, default=None, help=_help("render.burn"))
    sp.add_argument("--loudnorm", type=parse_loudnorm, default=None, metavar="LUFS|off", help=_help("render.loudnorm"))
    sp.add_argument("--force", action="store_true", help=_help("render.force"))
    sp.add_argument("--no-verify", action="store_true", help=_help("render.no_verify"))
    sp.add_argument("--dry-run", action="store_true", help=_help("render.dry_run"))

    sp = add("export")
    sp.add_argument("plan", metavar="PLAN", help=_help("export.plan"))
    sp.add_argument("--format", required=True, choices=FORMATS, help=_help("export.format"))
    sp.add_argument("--output", metavar="ID", help=_help("export.output"))
    sp.add_argument("--cuts", choices=CUT_MODES, default="markers", help=_help("export.cuts"))
    sp.add_argument("-o", "--out", metavar="PATH", help=_help("export.out"))
    sp.add_argument("--force", action="store_true", help=_help("export.force"))
    sp.add_argument("--dry-run", action="store_true", help=_help("export.dry_run"))

    sp = add("check")
    sp.add_argument("target", metavar="TARGET", help=_help("check.target"))
    sp.add_argument("--platform", choices=("auto", *PLATFORMS), default="auto", help=_help("check.platform"))
    sp.add_argument("--output", metavar="ID", help=_help("check.output"))
    sp.add_argument("--no-asr", action="store_true", help=_help("check.no_asr"))
    sp.add_argument("--save", metavar="PATH", help=_help("check.save"))
    return p


# ================================================================== 共用的检查与读写


def _need_ffmpeg() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise CliError("missing_ffmpeg", f"找不到 {'、'.join(missing)}", "brew install ffmpeg，然后 autocut doctor", EXIT_DEPENDENCY)


def _need_files(paths: Sequence[Path], what: str = "素材") -> None:
    for p in paths:
        if not p.exists():
            raise CliError("not_found", f"找不到{what} {p}（当前目录 {Path.cwd()}）", f"检查路径；{MEDIA_HINT}", EXIT_INPUT)
        if p.is_dir():
            raise CliError("not_found", f"{p} 是目录，不是{what}文件", "把目录里的文件逐个列出来，例如 clips/*.mp4", EXIT_INPUT)


def _plan_out(arg: str | None) -> Path | None:
    if arg is None:
        return None
    if not arg.endswith(".json"):
        raise CliError("bad_option", f"-o {arg}：方案文件名要以 .json 结尾",
                       "例如 -o talk.v2.autocut.json（render、check 靠 .json 后缀认出方案）", EXIT_USAGE)
    return Path(arg)


def _load_plan(arg: str | Path) -> Plan:
    p = Path(arg)
    if not p.exists():
        raise CliError("plan_not_found", f"找不到方案文件 {arg}（当前目录 {Path.cwd()}）",
                       "先用 cut / highlights / montage / ytpmv / narrate 生成方案；方案文件名形如 <素材名>.autocut.json", EXIT_INPUT)
    if p.suffix != ".json":
        raise CliError("not_a_plan", f"{arg} 不是方案文件（方案是 .json）",
                       f"素材要先生成方案，例如 autocut cut {_q(arg)}；再对生成的 .autocut.json 用这个命令", EXIT_INPUT)
    try:
        return Plan.load(p)
    except PlanError as e:
        raise CliError("plan_invalid", f"方案文件不合法：{e}",
                       "如果是手改坏的，用配方重新生成；改方案请用 autocut set（会校验）", EXIT_INPUT) from None


def _pick_output(plan: Plan, oid: str | None) -> Output:
    if oid is None:
        return plan.outputs[0]
    for o in plan.outputs:
        if o.id == oid:
            return o
    ids = [o.id for o in plan.outputs]
    close = difflib.get_close_matches(oid, ids, n=1)
    raise CliError("unknown_output", f"方案里没有输出 {oid!r}",
                   f"可选：{'、'.join(ids)}" + (f"；是不是 --output {close[0]}" if close else ""), EXIT_USAGE)


def _check_sources(plan: Plan, outputs: Sequence[Output]) -> None:
    used = {s.source for o in outputs for s in o.segments} | {b.source for o in outputs for b in o.beds}
    for sid in sorted(used):
        p = plan.source_path(sid)
        if not p.exists():
            raise CliError("source_missing", f"方案引用的素材不存在：{p}（sources.{sid}.path={plan.sources[sid].path!r}）",
                           "把素材放回原处；相对路径以方案所在目录为准。也可以在审阅页里重新定位素材", EXIT_INPUT)


@dataclass
class _PlanTarget:
    """配方要写的方案文件的现状：已有方案时，新方案接着它的 revision 往上加（审阅页靠 revision 发现外部修改）。"""

    base_rev: int = 0
    edits: int = 0
    exists: bool = False


def _prepare_plan_target(target: Path, *, force: bool, dry_run: bool) -> _PlanTarget:
    """配方跑之前就检查目标方案（免得白跑几分钟）。已经存在且没加 --force：失败（plan_exists），保护人在审阅页里的修改。"""
    if not target.exists():
        return _PlanTarget()
    rev: int | None = None
    edits = 0
    why = ""
    try:
        old = Plan.load(target)
        rev = old.revision
        edits = sum(1 for o in old.outputs for s in o.segments if s.evidence.get("edited"))
    except PlanError as e:
        why = f"，而且读不了（{_first_line(e)}）"
    if not force and not dry_run:
        stem = target.name.removesuffix(".json").removesuffix(".autocut")
        state = f"rev {rev}，{edits} 个片段被人改过" if rev is not None else "不是能读的 autocut 方案"
        raise CliError("plan_exists", f"方案 {_rel(target)} 已经存在（{state}）{why}，不覆盖",
                       f"要保留它就换个名字：-o {stem}.v2.autocut.json；确定要重新生成、丢掉旧方案{'和人改过的地方' if edits else ''}就加 --force",
                       EXIT_INPUT)
    return _PlanTarget(base_rev=rev or 0, edits=edits, exists=True)


def _commit_plan(plan: Plan, tgt: _PlanTarget, *, dry_run: bool) -> list[str]:
    plan.revision = tgt.base_rev
    if dry_run:
        if tgt.exists:
            return [f"方案 {_rel(plan.path)} 已经存在（rev {tgt.base_rev}，{tgt.edits} 个片段被人改过）："
                    "真正写入时要加 --force（覆盖）或 -o 新名字.json"]
        return []
    notes = [f"覆盖了旧方案（rev {tgt.base_rev}）里 {tgt.edits} 个人改过的片段（--force）"] if tgt.edits else []
    plan.save()
    log.info("方案已写入 %s（rev %d）", plan.path, plan.revision)
    return notes


# ---- 取消（长命令：render、transcribe）


def _child_pids() -> list[int]:
    """本进程的直接子进程（按父进程号精确匹配，不按名字）。"""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    me = os.getpid()
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == str(me):
            pids.append(int(parts[0]))
    return pids


def _stop_children(grace: float = 2.0) -> list[int]:
    """停掉自己启动的子进程（ffmpeg、whisper-cli）：先 SIGTERM，等一会儿还在就 SIGKILL。返回停掉的 pid。"""
    pids = _child_pids()
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    alive = set(pids)
    while alive and time.monotonic() < deadline:
        for pid in list(alive):
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done = pid
            if done:
                alive.discard(pid)
        if alive:
            time.sleep(0.05)
    for pid in alive:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    return pids


@contextlib.contextmanager
def _cancellable(command: str) -> Iterator[None]:
    """长命令：先在 stderr 写 pid；收到 SIGTERM 时停掉子进程，抛 _Cancelled（调用方删掉写了一半的文件）。"""
    pid = os.getpid()
    sys.stderr.write(f"pid={pid}（取消：kill {pid}）\n")
    sys.stderr.flush()

    def on_term(signum: int, frame: Any) -> None:
        stopped = _stop_children()
        log.warning("%s 收到 SIGTERM：停掉子进程 %s，正在清理", command, stopped or "（无）")
        raise _Cancelled(command)

    try:
        old = signal.signal(signal.SIGTERM, on_term)
    except ValueError:  # 不在主线程（例如被别的程序当库调用）：不接管信号
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _source_warnings(plan: Plan) -> list[str]:
    multi = len(plan.sources) > 1
    return [f"{sid}：{w}" if multi else w for sid, s in plan.sources.items() for w in s.warnings]


def _plan_head(plan: Plan, dry_run: bool) -> dict[str, Any]:
    head: dict[str, Any] = {"plan": _rel(plan.path), "revision": plan.revision}
    if dry_run:
        head["dry_run"] = True
    return head


def _notice(summary: dict[str, Any], plan: Plan) -> dict[str, str]:
    text = _summary_notice(summary, _q(_rel(plan.path)))
    return {"notice": text} if text else {}


def _review_hint(P: str) -> dict[str, str]:
    return _nx(f"autocut review {P}", "让人在浏览器里核对（推荐）：交给用户自己运行，它会一直运行；你要替用户打开就加 --detach")


def _recipe_next(plan: Plan, ui: Ui, dry_run: bool, summary: dict[str, Any], extra: Sequence[dict[str, str]] = ()) -> list[dict[str, str]]:
    if dry_run:
        return [_nx(_rerun_without(ui.argv, "--dry-run"), "去掉 --dry-run，真正写出方案")]
    P = _q(_rel(plan.path))
    nxt: list[dict[str, str]] = []
    if summary.get("degraded"):
        nxt.append(_nx("autocut doctor", "查缺什么、怎么补；补好后重跑这条命令"))
    n_unc = summary.get("uncertain_total") or 0
    if n_unc:
        nxt.append(_nx(f"autocut show {P} --filter uncertain", f"逐条看拿不准的 {n_unc} 处"))
    nxt.extend(extra)
    nxt.append(_review_hint(P))
    nxt.append(_nx(f"autocut render {P}", "直接出片"))
    return nxt


# ================================================================== 摘要（配方命令和 show 共用）


def _out_times(o: Output) -> list[tuple[Segment, float]]:
    """每个片段在成片里的位置：保留的是它的起点，剪掉的是剪口所在的时刻。"""
    rows, t = [], 0.0
    for s in o.segments:
        rows.append((s, t))
        if s.action == "keep":
            t += s.out_duration
    return rows


def _uncertain_item(s: Segment, out_at: float, multi_source: bool) -> dict[str, Any]:
    item: dict[str, Any] = {"id": s.id, "at": _r(s.start), "out_at": _r(out_at), "action": s.action, "reason": s.reason,
                            "confidence": _r(s.confidence), "detail": _trunc(s.detail, DETAIL_MAX), "text": _trunc(s.text, TEXT_MAX)}
    if multi_source:
        item["source"] = s.source
    return item


def _uncertain_block(plan: Plan, o: Output) -> dict[str, Any]:
    unc = [(s, at) for s, at in _out_times(o) if s.uncertain]
    multi = len(plan.sources) > 1
    return {"uncertain": [_uncertain_item(s, at, multi) for s, at in unc[:UNCERTAIN_LIMIT]], "uncertain_total": len(unc)}


def _asr_missing(plan: Plan, sids: Sequence[str] | None = None) -> bool:
    ids = sids if sids is not None else [k for k, v in plan.sources.items() if v.kind != "image"]
    return any(plan.sources[k].words is None for k in ids)


def _cut_summary(plan: Plan, o: Output) -> dict[str, Any]:
    cut = [s for s in o.segments if s.action == "cut"]
    wants_text = plan.options.get("retakes", True) or plan.options.get("fillers", True)
    return {
        "duration": _r(o.duration),
        "source_span": _r(sum(s.src_duration for s in o.segments)),
        "removed": _r(sum(s.src_duration for s in cut)),
        "cut": dict(Counter(s.reason for s in cut)),
        **_uncertain_block(plan, o),
        "degraded": bool(wants_text and _asr_missing(plan)),
    }


def _span(o: Output) -> tuple[float, float]:
    return (min(s.start for s in o.segments), max(s.end for s in o.segments)) if o.segments else (0.0, 0.0)


def _is_bait(o: Output) -> bool:
    return any(BAIT_MARK in r for r in o.reasons)


def _is_weak(o: Output) -> bool:
    return any(r.startswith(WEAK_MARK) for r in o.reasons)


def _highlights_summary(plan: Plan) -> dict[str, Any]:
    cands = []
    for o in plan.outputs:
        a, b = _span(o)
        item: dict[str, Any] = {"id": o.id, "start": _r(a), "end": _r(b), "duration": _r(o.duration), "score": o.score,
                                "selected": o.selected, "title": _trunc(o.title, 40), "reasons": [_trunc(r, 70) for r in o.reasons[:4]]}
        if _is_bait(o):
            item["bait"] = True
        if _is_weak(o):
            item["weak"] = True
        if any(s.uncertain for s in o.segments):
            item["uncertain"] = True
        cands.append(item)
    sel = [o for o in plan.outputs if o.selected]
    return {
        "requested": plan.options.get("n"),
        "selected": [o.id for o in sel],
        "candidates": cands[:CANDIDATE_LIMIT],
        "candidates_total": len(cands),
        "weak": [o.id for o in plan.outputs if _is_weak(o)],
        "baits": sum(1 for o in plan.outputs if _is_bait(o)),
        "duration_selected": _r(sum(o.duration for o in sel)),
        "uncertain_total": sum(1 for o in sel for s in o.segments if s.uncertain),
        "degraded": _asr_missing(plan),
    }


def _summary_notice(summary: dict[str, Any], plan_arg: str) -> str | None:
    """摘要里的列表为空、或者结果不够数时，给一句明确的话（不让 agent 去猜空列表是什么意思）。"""
    if "requested" in summary:
        k, n = len(summary["selected"]), summary["requested"]
        if n and k < n:
            weak = summary["weak"]
            return (f"只选中 {k} 条（要求 {n} 条）：没有拿弱候选凑数"
                    + (f"；弱候选 {'、'.join(weak)} 只列出没选，确认可用再 autocut set {plan_arg} {weak[0]} --select" if weak else ""))
        return None
    if summary.get("uncertain_total") == 0:
        return f"0 处拿不准：全部有把握（把握度都 ≥ {UNCERTAIN_BELOW:g}）"
    return None


def _montage_summary(plan: Plan, o: Output) -> dict[str, Any]:
    tl = o.timeline()
    visual = [k for k, v in plan.sources.items() if k != "music" and v.kind != "audio"]
    return {
        "duration": _r(o.duration),
        "bpm": plan.options.get("bpm"),
        "bars": plan.options.get("bars"),
        "cut_every": plan.options.get("cut_every"),
        "shots_total": len(tl),
        "sources_used": dict(Counter(s.source for s, _, _ in tl)),
        "sources_total": len(visual),
        "shots": [{"id": s.id, "at": _r(t0), "slot": _r(t1 - t0, 3), "source": s.source, "start": _r(s.start)}
                  for s, t0, t1 in tl[:LIST_LIMIT]],
        **_uncertain_block(plan, o),
    }


def _ytpmv_summary(plan: Plan, o: Output) -> dict[str, Any]:
    mats = plan.options.get("materials") or []
    texts = [m.get("text", "") for m in mats if m.get("text")]
    return {
        "duration": _r(o.duration),
        "bpm": plan.options.get("bpm"),
        "bars": plan.options.get("bars"),
        "placements": len(o.kept()),
        "distinct": len({(s.start, s.end) for s in o.kept()}),
        "syllables": len(mats),
        "syllable_text": _trunc(" ".join(texts), 120),
        "syllables_from": plan.options.get("syllables_from"),
        "pitch": bool(plan.options.get("pitch")),
        **_uncertain_block(plan, o),
        "degraded": plan.options.get("syllables_from") == "energy",
    }


def _narrate_summary(plan: Plan, o: Output) -> dict[str, Any]:
    narr = next((b for b in o.beds if b.role == "narration"), None)
    hook = next((x for x in o.overlays if x.kind == "text"), None)
    images = []
    for s, t0, t1 in o.timeline()[:LIST_LIMIT]:
        img: dict[str, Any] = {"id": s.id, "source": s.source, "at": _r(t0), "hold": _r(t1 - t0)}
        if s.evidence.get("sentences"):
            img["sentences"] = s.evidence["sentences"]
        images.append(img)
    return {
        "duration": _r(o.duration),
        "narration": _r((narr.end or 0.0) - narr.start) if narr else None,
        "sentences": plan.options.get("sentences"),
        "voice": plan.options.get("voice"),
        "size": f"{o.width}x{o.height}" if o.width and o.height else None,
        "images": images,
        "hook": {"text": _trunc(hook.text, 40), "duration": _r(hook.duration)} if hook else None,
        "music": any(b.role == "music" for b in o.beds),
        **_uncertain_block(plan, o),
    }


def _output_summary(plan: Plan, o: Output) -> dict[str, Any]:
    """通用的单条输出摘要（高光的某一条、手工方案）。"""
    cut = [s for s in o.segments if s.action == "cut"]
    a, b = _span(o)
    d: dict[str, Any] = {"duration": _r(o.duration), "start": _r(a), "end": _r(b), "segments": len(o.segments),
                         "kept": len(o.kept()), "cut": dict(Counter(s.reason for s in cut)), "selected": o.selected}
    if o.title:
        d["title"] = o.title
    if o.score is not None:
        d["score"] = o.score
    if o.reasons:
        d["reasons"] = [_trunc(r, 90) for r in o.reasons[:6]]
    d.update(_uncertain_block(plan, o))
    return d


def _summary_for(plan: Plan, output_id: str | None) -> dict[str, Any]:
    if plan.recipe == "highlights" and output_id is None:
        return _highlights_summary(plan)
    o = _pick_output(plan, output_id)
    fn = {"cut": _cut_summary, "montage": _montage_summary, "ytpmv": _ytpmv_summary, "narrate": _narrate_summary}.get(plan.recipe)
    return fn(plan, o) if fn else _output_summary(plan, o)


# ================================================================== 命令：doctor / analyze


def _run_quiet(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("%s 失败：%s", cmd[0], e)
        return ""
    return r.stdout


def _ff_names(text: str) -> set[str]:
    """`ffmpeg -filters` / `-encoders` 的输出 → 名字集合（每行：标志位 名字 说明）。"""
    out = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Z.|]{2,6}", parts[0]):
            out.add(parts[1])
    return out


# "Tingting (中文（中国大陆）) zh_CN    # 你好" / "Bad News            en_US    # Hello"
_SAY_LINE = re.compile(r"^(?P<name>.+?)(?:\s+\([^)]*\)+)?\s+(?P<locale>[a-z]{2,3}_[A-Za-z0-9]+)\s+#")


def say_voices() -> list[tuple[str, str]]:
    """本机 say 的声音：[(名字, locale)]；没有 say 时返回空列表。"""
    if shutil.which("say") is None:
        return []
    voices: list[tuple[str, str]] = []
    for line in _run_quiet(["say", "-v", "?"]).splitlines():
        m = _SAY_LINE.match(line)
        if m and (m.group("name").strip(), m.group("locale")) not in voices:
            voices.append((m.group("name").strip(), m.group("locale")))
    return voices


def cmd_doctor(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    from autocut import transcribe

    missing: list[str] = []
    fix: list[dict[str, str]] = []
    notes: list[str] = []

    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    ffmpeg: dict[str, Any] = {"path": ff, "ffprobe": fp, "version": None, "filters": {}, "encoders": {}}
    if ff:
        first = _run_quiet([ff, "-hide_banner", "-version"]).splitlines()[:1]
        m = re.search(r"version\s+(\S+)", first[0]) if first else None
        ffmpeg["version"] = m.group(1) if m else None
        have = _ff_names(_run_quiet([ff, "-hide_banner", "-filters"]))
        ffmpeg["filters"] = {k: k in have for k in FFMPEG_FILTERS}
        ffmpeg["encoders"] = {"libx264": "libx264" in _ff_names(_run_quiet([ff, "-hide_banner", "-encoders"]))}
        lacking = [k for k, ok in ffmpeg["filters"].items() if not ok] + [k for k, ok in ffmpeg["encoders"].items() if not ok]
        if lacking:
            missing.append("ffmpeg 滤镜：" + "、".join(lacking))
            fix.append({"for": "ffmpeg 滤镜", "cmd": "brew reinstall ffmpeg", "download": "约 100 MB（Homebrew 的 ffmpeg 自带 libass、rubberband、x264）"})
            notes.extend(f"缺 {k}：{FFMPEG_FILTERS.get(k, 'H.264 编码（出片）')}不可用" for k in lacking)
    if not ff or not fp:
        missing.append("ffmpeg")
        fix.append({"for": "ffmpeg", "cmd": "brew install ffmpeg", "download": "约 100 MB（含依赖）"})
        notes.append("没有 ffmpeg/ffprobe：除了 show、set 以外的命令都用不了")

    cli, model = transcribe.whisper_cli(), transcribe.find_model()
    asr: dict[str, Any] = {"available": bool(cli and model), "model": str(model) if model else None,
                           "model_tag": transcribe.model_tag() if model else None, "cli": cli}
    if not cli:
        missing.append("whisper-cli")
        fix.append({"for": "whisper-cli", "cmd": "brew install whisper-cpp", "download": "约 10 MB"})
    if not model:
        missing.append("语音识别模型")
        dest = transcribe.USER_MODELS / "ggml-large-v3-turbo.bin"
        fix.append({"for": "语音识别模型", "cmd": f"mkdir -p {_q(dest.parent)} && curl -L -o {_q(dest)} {WHISPER_MODEL_URL}",
                    "download": "约 1.6 GB（计费网络先别下；也可以设 AUTOCUT_WHISPER_MODEL 指向已有的 ggml-*.bin）"})
    if not asr["available"]:
        notes.append("没有语音识别：cut 只剪停顿（不检测重说和口头禅），highlights 没有关键词信号，ytpmv 的音节没有文字")
    elif asr["model_tag"] and not asr["model_tag"].startswith("large-v3-turbo"):
        notes.append(f"当前模型是 {asr['model_tag']}：词级时间戳不如 ggml-large-v3-turbo.bin 准，剪口可能偏")

    voices = say_voices()
    names = {n.casefold() for n, _ in voices}
    tts: dict[str, Any] = {"say": shutil.which("say") is not None, "voices": [v for v in DEFAULT_VOICES if v.casefold() in names],
                           "voices_total": len(voices)}
    if not tts["say"]:
        missing.append("say")
        notes.append("没有 macOS 的 say：narrate（配音知识视频）用不了")
    else:
        for v in DEFAULT_VOICES:
            if v.casefold() not in names:
                missing.append(f"say 的声音 {v}")
                fix.append({"for": f"say 的声音 {v}", "cmd": "系统设置 → 辅助功能 → 朗读内容 → 系统声音 → 管理声音…，勾选 " + v,
                            "download": "约 100–500 MB"})
    log.info("doctor：ffmpeg=%s asr=%s say=%s 缺 %s", ffmpeg["version"], asr["available"], tts["say"], missing or "无")
    nxt = [_nx("autocut analyze 素材.mp4", "不知道用哪个配方时先看一眼素材")] if not missing else []
    res: dict[str, Any] = {"ffmpeg": ffmpeg, "asr": asr, "tts": tts, "missing": missing, "fix": fix}
    if not missing:
        res["notice"] = "都齐了：所有命令都能完整运行（语音识别、配音、出片）"
    return {**res, "notes": notes, "next": nxt}


def _atomic_write(path: Path, text: str) -> None:
    """先写同目录的临时文件再 rename：被取消或出错时不会留下写了一半的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _word_cues(media: Any, words_path: Path, max_chars: int) -> list[Any]:
    """逐词文字稿 → 一句一句（借 captions 的断句：按标点和停顿切，max_chars 控制一行多长）。"""
    from autocut.captions import cues_for
    from autocut.plan import Captions, Source

    src = Source(id="m", path=str(media.path), duration=media.duration, words=str(words_path))
    out = Output("t", segments=[Segment("s1", "m", 0.0, media.duration)], captions=Captions(enabled=True, max_chars=max_chars))
    return cues_for(Plan(recipe="manual", sources={"m": src}, outputs=[out]), out)


def _no_asr_error() -> CliError:
    from autocut import transcribe

    reason = transcribe.unavailable_reason() or "语音识别不可用"
    return CliError("missing_asr", f"没有可用的语音识别：{reason}",
                    f"{reason}。autocut 不会自动下载任何东西；`autocut doctor` 会列出具体命令和下载量。不要在磁盘上找别的模型或别的项目里的 whisper",
                    EXIT_DEPENDENCY)


_TRANSCRIPT_EXT = {"json": ".words.json", "srt": ".srt", "txt": ".txt"}


def cmd_transcribe(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    path = Path(args.media)
    _need_files([path])
    from autocut import transcribe
    from autocut.analysis import Media
    from autocut.captions import format_srt

    m = Media(path)
    if not m.info.audio:
        raise CliError("no_audio", f"{m.path.name} 没有音轨", "transcribe 需要有声音的素材", EXIT_INPUT)
    track = _track_index(args.track, m)
    if not transcribe.available():
        raise _no_asr_error()
    out = Path(args.out) if args.out else path.with_name(path.stem + _TRANSCRIPT_EXT[args.format])
    if out.exists() and not args.force and not args.dry_run:
        raise CliError("exists", f"{_rel(out)} 已存在", "加 --force 覆盖，或者 -o 写到别的路径", EXIT_INPUT)
    lang = _lang(args.lang)
    log.info("transcribe：%s lang=%s track=%s format=%s → %s", path.name, lang, track, args.format, out)
    t0 = time.perf_counter()
    with _cancellable("transcribe"), ui.activity.spin(f"transcribe · 识别 {path.name}"):
        words = m.words(lang, track)
        if words is None:
            raise _no_asr_error()
        words_path = m.words_cache_path(lang, track)
        sentences = _word_cues(m, words_path, max_chars=40) if words and words_path else []
        if args.format == "json":
            text = json.dumps(words, ensure_ascii=False, indent=1) + "\n"
        elif args.format == "srt":
            text = format_srt(_word_cues(m, words_path, max_chars=16) if words and words_path else [])
        else:
            text = "".join(c.text + "\n" for c in sentences)
        if not args.dry_run:
            _atomic_write(out, text)
    seconds = time.perf_counter() - t0
    n_words = sum(1 for w in words if any(ch.isalnum() for ch in w["text"]))
    payload: dict[str, Any] = {"path": _rel(out), "format": args.format, "model": transcribe.model_tag(), "language": lang or "auto",
                               "track": _track_label(m, track), "words": n_words, "duration": _r(m.duration),
                               "preview": [{"at": _r(c.start), "text": c.text} for c in sentences[:PREVIEW_LIMIT]],
                               "preview_total": len(sentences), "seconds": _r(seconds, 1)}
    if args.dry_run:
        payload["dry_run"] = True
    if not n_words:
        payload["notice"] = f"没有识别到说话（{_track_label(m, track)}）：素材可能没有人声，或人声在别的音轨上（用 --track 指定）"
    elif len(sentences) > PREVIEW_LIMIT:
        payload["notice"] = f"preview 只列了前 {PREVIEW_LIMIT}/{len(sentences)} 句；全文在 {_rel(out)}"
    P = _q(_rel(path))
    nxt = [_nx(_rerun_without(ui.argv, "--dry-run"), "真正写出文字稿")] if args.dry_run else [
        _nx(f"autocut cut {P}", "按文字稿剪停顿、重说和口头禅（结果有缓存，不会再识别一遍）")]
    return {**payload, "notes": [], "next": nxt}


def cmd_transcript(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    path = Path(args.media)
    _need_files([path])
    from autocut import clips, transcribe
    from autocut.analysis import Media

    if not transcribe.available():
        raise _no_asr_error()
    if not 1 <= args.limit <= 100:
        raise CliError("usage", f"--limit {args.limit} 不在 1–100 内", "一页最多 100 句，用 --offset 翻页", EXIT_USAGE)
    m = Media(path)
    log.info("transcript：%s offset=%s limit=%s around=%s", path.name, args.offset, args.limit, args.around)
    with _cancellable("transcript"), ui.activity.spin(f"transcript · 读 {path.name}"):
        ctx = clips.make_context(m, _lang(args.lang))
    pg = clips.page(ctx.sents, offset=args.offset, limit=args.limit, around=args.around)
    P = _q(_rel(path))
    nxt = []
    if pg["next_offset"] is not None:
        nxt.append(_nx(f"autocut transcript {P} --offset {pg['next_offset']} --limit {args.limit}", "下一页"))
    if pg["items"]:
        a, b = pg["items"][0]["id"], pg["items"][-1]["id"]
        nxt.append(_nx(f"autocut clip {P} --from {a} --to {b} --title … --reason …", "按句段 id 选一段做候选（时间由程序换算）"))
    notes = [] if pg["total"] else ["没有识别到说话"]
    return {"path": _rel(path), "duration": _r(m.duration), "language": _lang(args.lang) or "auto", **pg, "next": nxt, "notes": notes,
            **({"notice": "没有识别到说话：素材可能没有人声"} if not pg["total"] else {})}


def cmd_clip(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    from autocut import clips
    from autocut.analysis import Media
    from autocut.recipes._common import plan_path_for

    target = Path(args.target)
    if not (0.0 <= args.confidence <= 1.0):
        raise CliError("usage", f"--confidence {args.confidence} 不在 0–1 内", "例如 --confidence 0.8", EXIT_USAGE)
    aspect = None if args.aspect.lower() == "none" else args.aspect
    if target.suffix == ".json":
        plan = _load_plan(target)
        if plan.recipe != "clips":
            raise CliError("not_clips_plan", f"{target.name} 是 {plan.recipe} 方案，不是 clips 方案", "clip 只往 clips 方案里加候选；新建请直接给素材：autocut clip 素材.mp4 --from …", EXIT_INPUT)
        media_path = plan.source_path(next(iter(plan.sources)))
    else:
        _need_files([target])
        media_path = target
        out = Path(args.out) if args.out else plan_path_for(target, None)
        if out.exists() and not args.force:
            plan = _load_plan(out)  # 已有方案：继续往里加
            if plan.recipe != "clips":
                raise CliError("plan_exists", f"{_rel(out)} 已存在且是 {plan.recipe} 方案", "-o 写到别的路径，或 --force 覆盖（会丢掉旧方案）", EXIT_INPUT)
        else:
            plan = clips.new_plan(Media(target), out, aspect=aspect, language=_lang(args.lang))
    P = _q(_rel(plan.path))
    if args.expect_revision is not None and plan.revision != args.expect_revision:
        raise CliError("revision_conflict", f"方案已经被别人改过（现在是 rev {plan.revision}，你期望 rev {args.expect_revision}）", f"先 autocut show {P} 重新读", EXIT_INPUT)
    media = Media(media_path)
    ctx = clips.make_context(media, _lang(args.lang))
    log.info("clip：%s %s–%s length=%s forbid=%s", media_path.name, args.first, args.last, args.length, args.forbid)
    try:
        output, warns = clips.add_clip(plan, ctx, args.first, args.last, title=args.title, reason=args.reason, confidence=args.confidence, length=args.length,
                                       forbidden=list(args.forbid), banned_words=[], aspect=aspect, selected=args.select)
    except clips.ClipError as e:
        first = e.violations[0]
        raise CliError(first["code"], f"这条候选没过硬约束：{e}", "；".join(f"{v['code']}：{v['fix']}" for v in e.violations), EXIT_INPUT) from None
    seg = output.segments[0]
    if not args.dry_run:
        plan.validate()
        plan.save()
    payload = {"plan": _rel(plan.path), "revision": plan.revision, "output": output.id, "selected": output.selected, "title": output.title,
               "start": _r(seg.start), "end": _r(seg.end), "at": _mmss(seg.start), "duration": _r(output.duration, 1), "from": args.first, "to": args.last,
               "warnings": warns, "total_candidates": len(plan.outputs)}
    if args.dry_run:
        payload["dry_run"] = True
    nxt = [_nx(f"autocut set {P} {output.id} --select", "选中它（出片只出被选中的）")] if not output.selected else []
    nxt.append(_nx(f"autocut render {P} --output {output.id}", "出片（出片后自动核验）"))
    return {**payload, "next": nxt, "notes": [w["message"] for w in warns]}


def cmd_analyze(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    path = Path(args.media)
    _need_files([path])
    from autocut.recipes import narrate

    P = _q(_rel(path))
    suffix = path.suffix.lower()
    if suffix in narrate.SCRIPT_EXT:
        script = narrate.parse_script(path.read_text(encoding="utf-8"))
        sug = [_nx(f"autocut narrate {P} 图1.png 图2.png", f"文稿（{len(script.sentences)} 句）：配上图片做配音知识视频")]
        return {"path": _rel(path), "kind": "script", "sentences": len(script.sentences), "suggest": sug, "next": sug[:1], "notes": []}
    _need_ffmpeg()
    if suffix in narrate.IMAGE_EXT:
        sug = [_nx(f"autocut narrate 文稿.md {P}", "图片：配一份文稿做配音知识视频")]
        return {"path": _rel(path), "kind": "image", "suggest": sug, "next": sug[:1], "notes": []}

    from autocut.analysis import Media

    t0 = time.perf_counter()
    m = Media(path)
    info = m.info
    v = info.video
    voice = m.voice_track()
    out: dict[str, Any] = {"path": _rel(path), "kind": "video" if v else "audio", "duration": _r(info.duration)}
    if v:
        out["video"] = f"{v.width}x{v.height} {v.fps:.3g}fps {'VFR' if v.is_vfr else 'CFR'}" + (f" 旋转{v.rotation}°" if v.rotation else "")
    out["audio"] = [{"track": a.track_number + 1, "title": a.title, **({"voice_guess": True} if a.track_number == voice else {})}
                    for a in info.audio]
    notes: list[str] = []
    ratio: float | None = None
    bpm: float | None = None
    with ui.activity.spin(f"analyze · {path.name}"):
        if info.audio:
            regions = m.speech(voice).regions
            ratio = sum(e - s for s, e in regions) / info.duration if info.duration else 0.0
            if not v:
                b = m.beats()
                bpm = round(b.bpm, 1) if b.confidence >= 0.3 else None
                out["beat_confidence"] = _r(b.confidence)
    out["speech_ratio"] = _r(ratio)
    out["bpm"] = bpm
    if v and v.is_vfr:
        notes.append("可变帧率（录屏常见）：出片时会统一成恒定帧率，否则音画可能错位")
    chat = path.with_name(f"{path.stem}.chat.jsonl")
    if chat.exists():
        out["chat"] = _rel(chat)
    out["suggest"] = _suggest(path, info, voice, ratio, bpm, chat if chat.exists() else None)
    out["seconds"] = _r(time.perf_counter() - t0, 1)
    if not out["suggest"]:
        out["notice"] = "看不出明显适合哪个配方：说话不多、也不像音乐；按用户想要的效果选（autocut --help 有对照表）"
    log.info("analyze %s：%.1fs，语音占比 %s，bpm %s，%.2fs", path.name, info.duration, ratio, bpm, time.perf_counter() - t0)
    return {**out, "notes": notes, "next": out["suggest"][:1]}


def _suggest(path: Path, info: Any, voice: int | None, ratio: float | None, bpm: float | None, chat: Path | None) -> list[dict[str, str]]:
    """按素材特征建议配方（最多 3 条）：每条给出可以直接运行的命令和理由。"""
    P = _q(_rel(path))
    pct = f"{ratio:.0%}" if ratio is not None else "—"
    sug: list[dict[str, str]] = []
    if not info.video:
        if bpm:
            sug.append(_nx(f"autocut montage 素材1.mp4 素材2.mp4 --music {P}", f"像音乐（{bpm:g} BPM）：可以当卡点混剪的 BGM"))
            sug.append(_nx(f"autocut ytpmv 台词.mp4 --music {P}", "也可以当鬼畜的 BGM"))
        elif ratio and ratio >= 0.3:
            sug.append(_nx(f"autocut cut {P}", f"{pct} 的时间在说话，像播客或录音：剪停顿和口头禅"))
        return sug
    if not info.audio:
        return [_nx(f"autocut montage {P} 其他素材.mp4 --music bgm.mp3", "没有音轨：纯画面素材适合做卡点混剪")]
    if chat is not None or info.duration >= 1200:
        why = f"旁边有弹幕文件 {chat.name}：可以看弹幕爆发、识别抽奖刷屏" if chat else f"{_mmss(info.duration)} 的长录像：切几条短视频"
        sug.append(_nx(f"autocut highlights {P}" + (f" --chat {_q(_rel(chat))}" if chat else ""), why))
    if len(info.audio) >= 2 and voice is not None:
        titles = "/".join(a.title or str(a.track_number + 1) for a in info.audio)
        sug.append(_nx(f"autocut cut {P}", f"{len(info.audio)} 条音轨（{titles}），像 OBS 游戏实况：cut 会保留没解说但有动静的段落"))
    elif ratio is not None and ratio >= 0.3:
        sug.append(_nx(f"autocut cut {P}", f"{pct} 的时间在说话，像口播或录课：剪停顿、重说和口头禅"))
    if ratio is not None and ratio >= 0.3 and info.duration <= 20:
        sug.append(_nx(f"autocut ytpmv {P} --music bgm.mp3", "台词很短：也可以切成音节做鬼畜"))
    if ratio is not None and ratio < 0.3 and info.duration <= 120 and not sug:
        sug.append(_nx(f"autocut montage {P} 其他素材.mp4 --music bgm.mp3", f"很少说话（{pct}）：适合当卡点混剪的素材"))
    return sug[:3]


# ================================================================== 命令：配方


def cmd_cut(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    paths = [Path(p) for p in args.media]
    _need_files(paths)
    from autocut import transcribe
    from autocut.analysis import Media
    from autocut.recipes import cut as recipe
    from autocut.recipes._common import plan_path_for

    medias = [Media(p) for p in paths]
    for m in medias:
        if not m.info.audio:
            raise CliError("no_audio", f"{m.path.name} 没有音轨", "cut 需要有声音的素材；纯画面素材用 montage", EXIT_INPUT)
    track = _track_arg(args.track, medias)
    out = _plan_out(args.out)
    tgt = _prepare_plan_target(plan_path_for(paths[0], out), force=args.force, dry_run=args.dry_run)
    keep_action: bool | str = {"auto": "auto", "on": True, "off": False}[args.keep_action]
    log.info("cut：%s pace=%s track=%s retakes=%s fillers=%s protect=%s", [p.name for p in paths], args.pace, track,
             not args.no_retakes, not args.no_fillers, args.protect)
    t0 = time.perf_counter()
    with ui.activity.spin(f"cut · 分析 {paths[0].name}" + (f" 等 {len(paths)} 个素材" if len(paths) > 1 else "")):
        plan = recipe.build(paths, out=out, pace=args.pace, language=_lang(args.lang), track=track, retakes=not args.no_retakes,
                            fillers=not args.no_fillers, protect=args.protect, keep_action=keep_action)
    seconds = time.perf_counter() - t0
    notes = list(plan.notes) + _source_warnings(plan) + _commit_plan(plan, tgt, dry_run=args.dry_run)
    summary = _cut_summary(plan, plan.outputs[0])
    if summary["degraded"]:
        notes.append("没检测重说/口头禅（缺语音识别）：运行 autocut doctor 看缺什么，补好后重跑：" + _rerun_without(ui.argv, "--json"))
    src0 = next(iter(plan.sources.values()))
    has_words = any(s.words for s in plan.sources.values())
    return {
        **_plan_head(plan, args.dry_run),
        "summary": summary,
        "analysis": {"track": _track_label(medias[0], src0.analysis_track), "asr": transcribe.model_tag() if has_words else None,
                     "seconds": _r(seconds, 1)},
        "media": [_media_brief(m) for m in medias],
        **_notice(summary, plan),
        "notes": notes,
        "next": _recipe_next(plan, ui, args.dry_run, summary),
    }


def _lang(raw: str | None) -> str | None:
    return None if raw is None or raw.strip().lower() in ("", "auto") else raw.strip()


def _track_arg(raw: str | None, medias: Sequence[Any]) -> int | str | None:
    """--track：音轨号（1 起）或标题 → 配方要的（0 起的序号或标题）。越界 → 用法错误，列出现有音轨。"""
    if raw is None:
        return None
    v = raw.strip()
    for m in medias:
        titles = [a.title for a in m.info.audio]
        if v.isdigit() and 1 <= int(v) <= len(titles):
            continue
        if not v.isdigit() and any(t and t.strip().casefold() == v.casefold() for t in titles):
            continue
        listing = "、".join(f"{i + 1} {t or '（无标题）'}" for i, t in enumerate(titles))
        guess = m.voice_track()
        hint = guess + 1 if guess is not None else min(2, len(titles))
        raise CliError("track_out_of_range", f"{m.path.name} 只有 {len(titles)} 条音轨：{listing}", f"用 --track {hint}", EXIT_USAGE)
    return int(v) - 1 if v.isdigit() else v


def _track_index(raw: str | None, m: Any) -> int | None:
    """--track（1 起的序号或标题）→ 0 起的音轨序号；None = 按标题猜人声轨（猜不出来就是默认音轨）。"""
    v = _track_arg(raw, [m])
    if v is None:
        return m.voice_track()
    if isinstance(v, int):
        return v
    return next(a.track_number for a in m.info.audio if a.title and a.title.strip().casefold() == v.casefold())


def _track_label(m: Any, track: int | None) -> str:
    tracks = m.info.audio
    i = track if track is not None else 0
    title = tracks[i].title if i < len(tracks) else None
    return f"音轨 {i + 1}" + (f"（{title}）" if title else "") + ("，默认" if track is None and len(tracks) > 1 else "")


def _media_brief(m: Any) -> dict[str, Any]:
    info, v = m.info, m.info.video
    d: dict[str, Any] = {"path": _rel(m.path), "duration": _r(info.duration)}
    if v:
        d["video"] = f"{v.width}x{v.height} {v.fps:.3g}fps" + (" VFR" if v.is_vfr else "")
    d["tracks"] = [a.title or str(a.track_number + 1) for a in info.audio]
    return d


def cmd_highlights(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    vod = Path(args.vod)
    _need_files([vod])
    chat = Path(args.chat) if args.chat else None
    if chat is not None:
        _need_files([chat], "弹幕文件")
    from autocut.recipes import highlights as recipe
    from autocut.recipes._common import RecipeError, aspect_size, plan_path_for

    aspect = None if args.aspect.strip().lower() in ("source", "none", "") else args.aspect
    try:
        aspect_size(aspect)
    except RecipeError as e:
        raise CliError("bad_option", f"--aspect {args.aspect}：{e}", "例如 --aspect 9:16、--aspect 16:9，或 --aspect source 跟随素材", EXIT_USAGE) from None
    out = _plan_out(args.out)
    tgt = _prepare_plan_target(plan_path_for(vod, out), force=args.force, dry_run=args.dry_run)
    log.info("highlights：%s n=%d candidates=%s length=%s aspect=%s chat=%s", vod.name, args.n, args.candidates, args.length, aspect, chat)
    with ui.activity.spin(f"highlights · 分析 {vod.name}"):
        plan = recipe.build([vod], out=out, n=args.n, candidates=args.candidates, length=args.length, aspect=aspect,
                            chat=chat, language=_lang(args.lang), reframe=args.reframe)
    notes = list(plan.notes) + _source_warnings(plan) + _commit_plan(plan, tgt, dry_run=args.dry_run)
    sibling = vod.with_name(f"{vod.stem}.chat.jsonl")
    extra: list[dict[str, str]] = []
    if chat is None and sibling.exists():
        notes.append(f"旁边有弹幕文件 {_rel(sibling)}，这次没用；加 --chat 能看弹幕爆发、识别抽奖刷屏")
        extra.append(_nx(_rerun_without(ui.argv, "--json") + f" --chat {_q(_rel(sibling))}", "带上弹幕重跑"))
    summary = _highlights_summary(plan)
    if not args.dry_run:
        if summary["weak"] and len(summary["selected"]) < (summary["requested"] or 0):
            w = summary["weak"][0]
            extra.append(_nx(f"autocut set {_q(_rel(plan.path))} {w} --select", f"{w} 是弱候选（只有音量佐证）：先和用户确认可用再选上"))
        else:
            spare = next((c for c in summary["candidates"] if not c["selected"] and not c.get("bait") and not c.get("weak")), None)
            if spare:
                extra.append(_nx(f"autocut set {_q(_rel(plan.path))} {spare['id']} --select", f"想多要一条就选上 {spare['id']}"))
    nxt = _recipe_next(plan, ui, args.dry_run, summary, extra)
    return {**_plan_head(plan, args.dry_run), "summary": summary, **_notice(summary, plan), "notes": notes, "next": nxt}


def cmd_montage(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    clips = [Path(p) for p in args.clips]
    music = Path(args.music)
    _need_files(clips)
    _need_files([music], "BGM")
    from autocut.recipes import montage as recipe
    from autocut.recipes._common import plan_path_for

    out = _plan_out(args.out)
    tgt = _prepare_plan_target(plan_path_for(clips[0], out), force=args.force, dry_run=args.dry_run)
    log.info("montage：%d 个素材，music=%s length=%s cut_every=%s aspect=%s", len(clips), music.name, args.length, args.cut_every, args.aspect)
    with ui.activity.spin(f"montage · 分析 {len(clips)} 个素材和 {music.name}"):
        plan = recipe.build(clips, out=out, music=music, length=args.length, cut_every=args.cut_every, aspect=args.aspect,
                            music_start=args.music_start)
    notes = list(plan.notes) + _source_warnings(plan) + _commit_plan(plan, tgt, dry_run=args.dry_run)
    summary = _montage_summary(plan, plan.outputs[0])
    return {**_plan_head(plan, args.dry_run), "summary": summary, **_notice(summary, plan), "notes": notes,
            "next": _recipe_next(plan, ui, args.dry_run, summary)}


def cmd_ytpmv(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    clip, music = Path(args.clip), Path(args.music)
    _need_files([clip])
    _need_files([music], "BGM")
    from autocut.recipes import ytpmv as recipe
    from autocut.recipes._common import plan_path_for

    out = _plan_out(args.out)
    tgt = _prepare_plan_target(plan_path_for(clip, out), force=args.force, dry_run=args.dry_run)
    log.info("ytpmv：%s music=%s pattern=%s pitch=%s length=%s", clip.name, music.name, args.pattern, args.pitch, args.length)
    with ui.activity.spin(f"ytpmv · 切音节 {clip.name}"):
        plan = recipe.build([clip], out=out, music=music, pattern=args.pattern, pitch=args.pitch, length=args.length,
                            language=_lang(args.lang))
    notes = list(plan.notes) + _source_warnings(plan) + _commit_plan(plan, tgt, dry_run=args.dry_run)
    summary = _ytpmv_summary(plan, plan.outputs[0])
    extra = [] if args.dry_run else [_nx(f"autocut export {_q(_rel(plan.path))} --format fcpxml", "导出到剪辑软件里继续编排（推荐）")]
    return {**_plan_head(plan, args.dry_run), "summary": summary, **_notice(summary, plan), "notes": notes,
            "next": _recipe_next(plan, ui, args.dry_run, summary, extra)}


def cmd_narrate(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    _need_ffmpeg()
    script, images = Path(args.script), [Path(p) for p in args.images]
    _need_files([script], "文稿")
    _need_files(images)
    if args.music:
        _need_files([Path(args.music)], "BGM")
    from autocut.recipes import narrate as recipe
    from autocut.recipes._common import plan_path_for

    if script.suffix.lower() not in recipe.SCRIPT_EXT:
        raise CliError("bad_option", f"{script.name} 不是文稿（要 {'/'.join(sorted(recipe.SCRIPT_EXT))}）",
                       "第一个参数放文稿，后面放图片：autocut narrate script.md img1.png img2.png", EXIT_USAGE)
    if shutil.which("say") is None:
        raise CliError("missing_say", "找不到 macOS 的 say 命令", "narrate 用 say 配音，只能在 macOS 上运行", EXIT_DEPENDENCY)
    voice = args.voice or recipe.detect_voice(recipe.parse_script(script.read_text(encoding="utf-8")).sentences)
    _need_voice(voice)
    out = _plan_out(args.out)
    tgt = _prepare_plan_target(plan_path_for(script, out), force=args.force, dry_run=args.dry_run)
    log.info("narrate：%s + %d 个画面，voice=%s size=%s music=%s", script.name, len(images), voice, args.size, args.music)
    with ui.activity.spin(f"narrate · 配音 {script.name}"):
        plan = recipe.build([script, *images], out=out, voice=voice, size=args.size, music=args.music, rate=args.rate)
    notes = list(plan.notes) + _source_warnings(plan) + _commit_plan(plan, tgt, dry_run=args.dry_run)
    summary = _narrate_summary(plan, plan.outputs[0])
    nxt = _recipe_next(plan, ui, args.dry_run, summary)
    for n in nxt:
        if n["cmd"].startswith("autocut review"):
            n["why"] = "让人核对每张图和它对应的句子是否匹配（推荐）：交给用户自己运行；你要替用户打开就加 --detach"
    return {**_plan_head(plan, args.dry_run), "summary": summary, **_notice(summary, plan), "notes": notes, "next": nxt}


def _need_voice(voice: str) -> None:
    voices = say_voices()
    if not voices or any(n.casefold() == voice.casefold() for n, _ in voices):
        return  # 列不出声音时交给 say 自己报错（配方会附上可用的声音）
    names = [n for n, _ in voices]
    usable = [v for v in DEFAULT_VOICES if v in names] + [n for n, loc in voices if loc in ("zh_CN", "en_US")]
    usable = list(dict.fromkeys(usable))[:6] or names[:6]
    raise CliError("missing_voice", f"say 没有声音 {voice!r}",
                   f"系统设置 → 辅助功能 → 朗读内容 → 系统声音 → 管理声音…，下载 {voice}；或者 --voice 换一个已装的："
                   + "、".join(usable), EXIT_DEPENDENCY)


# ================================================================== 命令：show / set


def cmd_show(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    plan = _load_plan(args.plan)
    P = _q(_rel(plan.path))
    seg_mode = args.segments or args.filter is not None or args.around is not None
    head = {"plan": _rel(plan.path), "revision": plan.revision, "recipe": plan.recipe}
    if args.output == "all":
        if seg_mode:
            raise CliError("bad_option", "--output all 只能看摘要，不能和 --segments/--filter/--around 一起用",
                           f"列片段时指定一条输出，例如 --output {plan.outputs[0].id}", EXIT_USAGE)
        outs = [{"id": o.id, "title": o.title or None, "duration": _r(o.duration), "selected": o.selected, "score": o.score,
                 "segments": len(o.segments), "uncertain": sum(1 for s in o.segments if s.uncertain)} for o in plan.outputs]
        return {**head, "output": "all", "outputs": outs[:LIST_LIMIT], "total": len(outs), "notes": plan.notes,
                "next": [_nx(f"autocut show {P} --output {plan.outputs[0].id}", "看某一条的摘要")]}
    if not seg_mode:
        summary = _summary_for(plan, args.output)
        out_id = args.output or (plan.outputs[0].id if plan.recipe != "highlights" else None)
        nxt: list[dict[str, str]] = []
        if summary.get("uncertain_total"):
            nxt.append(_nx(f"autocut show {P} --filter uncertain" + (f" --output {out_id}" if args.output else ""),
                           f"逐条看拿不准的 {summary['uncertain_total']} 处"))
        nxt += [_review_hint(P), _nx(f"autocut render {P}", "出片")]
        return {**head, "output": out_id, "summary": summary, **_notice(summary, plan), "notes": plan.notes, "next": nxt}

    if args.limit > LIST_LIMIT_MAX:
        raise CliError("bad_option", f"--limit {args.limit} 太大：一页最多 {LIST_LIMIT_MAX} 条（JSON 太长会占满上下文）",
                       "用 --offset 翻页，或者 --filter uncertain 只看拿不准的", EXIT_USAGE)
    o = _pick_output(plan, args.output)
    flt = args.filter or "all"
    timebase = "source" if args.source_time else "output"
    picked = [(s, at) for s, at in _out_times(o) if _matches(s, flt) and _near(s, at, args.around, timebase)]
    total = len(picked)
    page = picked[args.offset: args.offset + args.limit]
    nxt_off = args.offset + args.limit if args.offset + args.limit < total else None
    multi = len(plan.sources) > 1
    payload: dict[str, Any] = {**head, "output": o.id, "filter": flt, "segments": [_seg_row(s, at, multi) for s, at in page],
                               "total": total, "showing": len(page), "offset": args.offset, "next_offset": nxt_off}
    if args.around is not None:
        payload["around"] = args.around
        payload["timebase"] = timebase
    base = f"autocut show {P}" + (f" --output {o.id}" if args.output else "") + (f" --filter {flt}" if flt != "all" else " --segments")
    nxt = []
    if nxt_off is not None:
        shown = f"{len(page)}/{total} 条" if args.offset == 0 else f"第 {args.offset + 1}–{args.offset + len(page)} 条（共 {total} 条）"
        payload["notice"] = f"只显示了 {shown}；用 --offset {nxt_off} 翻页" + ("，或者 --filter uncertain 只看拿不准的" if flt != "uncertain" else "")
        nxt.append(_nx(f"{base} --offset {nxt_off}", "下一页"))
    elif not total:
        payload["notice"] = _empty_segments_notice(o, flt, args.around, timebase)
    first = page[0][0] if page else None
    if first is not None and flt == "uncertain":
        flip = "keep" if first.action == "cut" else "cut"
        nxt.append(_nx(f"autocut set {P} {first.id} --{flip}" + (f" --output {o.id}" if len(plan.outputs) > 1 else ""),
                       f"把 {first.id} 改成{'保留' if flip == 'keep' else '剪掉'}（改错了再 set 回去）"))
    nxt += [_review_hint(P)]
    return {**payload, "notes": [], "next": nxt}


def _matches(s: Segment, flt: str) -> bool:
    if flt == "all":
        return True
    if flt == "uncertain":
        return s.uncertain
    if flt in ("cut", "keep"):
        return s.action == flt
    return s.reason == flt


def _near(s: Segment, out_at: float, t: float | None, timebase: str) -> bool:
    """片段和 t ±AROUND 有没有交集。成片时间：保留的片段占 [out_at, out_at+时长]，剪掉的片段只是 out_at 处的一个接缝。"""
    if t is None:
        return True
    if timebase == "source":
        return s.end >= t - AROUND and s.start <= t + AROUND
    out_end = out_at + (s.out_duration if s.action == "keep" else 0.0)
    return out_end >= t - AROUND and out_at <= t + AROUND


def _empty_segments_notice(o: Output, flt: str, around: float | None, timebase: str) -> str:
    if around is not None:
        if timebase == "output" and around > o.duration + AROUND:
            return f"成片只有 {_mmss(o.duration)}，{_mmss(around)} 超出了成片；如果说的是素材里的时间，加 --source-time"
        return f"{'成片' if timebase == 'output' else '素材'}时间 {_mmss(around)} 前后 {AROUND:g}s 没有" + (f" {flt} 的" if flt != "all" else "") + "片段"
    if flt == "uncertain":
        return f"0 处拿不准：全部有把握（把握度都 ≥ {UNCERTAIN_BELOW:g}）"
    return f"没有 {flt} 的片段" if flt != "all" else "这条输出没有片段"


def _seg_row(s: Segment, out_at: float, multi_source: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"id": s.id, "start": _r(s.start, 3), "end": _r(s.end, 3), "out_at": _r(out_at), "action": s.action,
                           "reason": s.reason, "confidence": _r(s.confidence), "detail": _trunc(s.detail, DETAIL_MAX),
                           "text": _trunc(s.text, TEXT_MAX), "edited": bool(s.evidence.get("edited"))}
    if multi_source:
        row["source"] = s.source
    if s.slot is not None:
        row["slot"] = _r(s.slot, 3)
    return row


def cmd_set(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    plan = _load_plan(args.plan)
    P = _q(_rel(plan.path))
    if args.expect_revision is not None and plan.revision != args.expect_revision:
        raise CliError("revision_conflict", f"方案已经被别人改过（现在是 rev {plan.revision}，你期望 rev {args.expect_revision}）",
                       f"先 autocut show {P} 重新读，再决定改不改", EXIT_INPUT)
    edge = args.start is not None or args.end is not None
    if not args.op and not edge:
        raise CliError("usage", "没说要改成什么", f"加 --keep、--cut、--start/--end、--select 或 --deselect，例如 autocut set {P} {args.ids[0]} --keep", EXIT_USAGE)
    if args.op and edge:
        raise CliError("usage", f"--{args.op} 不能和 --start/--end 一起用", "一次只做一种修改：先 --keep/--cut，再单独挪边界", EXIT_USAGE)
    rev0 = plan.revision
    dur0 = {o.id: o.duration for o in plan.outputs}
    touched: list[Output] = []
    if args.op in ("select", "deselect"):
        changed, unchanged = _apply_select(plan, args.ids, args.op == "select")
    elif args.op:
        changed, unchanged, touched = _apply_action(plan, args.ids, args.op, args.output)
    else:
        if len(args.ids) != 1:
            raise CliError("usage", "--start/--end 一次只能对一个片段用", f"例如 autocut set {P} {args.ids[0]} --end 48.5", EXIT_USAGE)
        changed, unchanged, touched = _apply_boundary(plan, args.ids[0], args.start, args.end, args.output)
    notes: list[str] = []
    if changed and not args.dry_run:
        disk_rev = _disk_revision(plan.path)
        if disk_rev != rev0:
            raise CliError("revision_conflict", f"方案在这期间被别人改过（读的时候是 rev {rev0}，现在是 rev {disk_rev}）",
                           f"先 autocut show {P} 重新读，再决定改不改", EXIT_INPUT, retry=True)
        plan.validate()
        plan.save()
        log.info("set：%d 处修改，rev %d → %d", len(changed), rev0, plan.revision)
    payload: dict[str, Any] = {"plan": _rel(plan.path), "revision": plan.revision, "changed": changed}
    if not changed:
        payload["notice"] = f"没有变化：{'、'.join(unchanged)} 已经是要的状态，方案没有保存（rev {plan.revision} 不变）"
    if unchanged:
        payload["unchanged"] = unchanged
    if touched:
        payload["duration"] = {"before": _r(sum(dur0[o.id] for o in touched)), "after": _r(sum(o.duration for o in touched))}
    if args.op in ("select", "deselect"):
        payload["selected"] = [o.id for o in plan.outputs if o.selected]
    if args.dry_run:
        payload["dry_run"] = True
    nxt: list[dict[str, str]] = []
    if args.dry_run and changed:
        nxt.append(_nx(_rerun_without(ui.argv, "--dry-run"), "去掉 --dry-run，真正保存"))
    left = sum(1 for o in plan.outputs for s in o.segments if s.uncertain)
    if left:
        nxt.append(_nx(f"autocut show {P} --filter uncertain", f"还有 {left} 处拿不准"))
    nxt.append(_nx(f"autocut render {P}", "出片"))
    return {**payload, "notes": notes, "next": nxt}


def _disk_revision(path: Path | None) -> int | None:
    try:
        return int(json.loads(Path(path).read_text()).get("revision", 0)) if path else None
    except (OSError, ValueError):
        return None


def _segment_index(plan: Plan) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for o in plan.outputs:
        for s in o.segments:
            idx.setdefault(s.id, []).append(o.id)
    return idx


def _unknown_id(plan: Plan, sid: str, pool: Sequence[str], what: str) -> CliError:
    close = difflib.get_close_matches(sid, pool, n=3, cutoff=0.5)
    P = _q(_rel(plan.path))
    return CliError("unknown_id", f"方案里没有{what} id {sid!r}",
                    f"用 autocut show {P} --segments 查看" + (f"；相近的 id：{'、'.join(close)}" if close else ""), EXIT_INPUT)


def _resolve_segment(plan: Plan, sid: str, output_id: str | None) -> tuple[Output, Segment]:
    out_ids = [o.id for o in plan.outputs]
    if output_id is not None:
        o = _pick_output(plan, output_id)
        for s in o.segments:
            if s.id == sid:
                return o, s
        raise _unknown_id(plan, sid, [s.id for s in o.segments], f"（输出 {o.id} 里）片段")
    owners = _segment_index(plan).get(sid, [])
    if len(owners) == 1:
        o = plan.output(owners[0])
        return o, o.find(sid)[1]
    if len(owners) > 1:
        raise CliError("ambiguous_id", f"片段 id {sid!r} 在 {'、'.join(owners)} 里都有",
                       f"用 --output 指定哪条，例如 --output {owners[0]}", EXIT_USAGE)
    if sid in out_ids:
        raise CliError("usage", f"{sid} 是输出 id，--keep/--cut/--start/--end 只对片段生效",
                       f"选或取消候选用 autocut set {_q(_rel(plan.path))} {sid} --select / --deselect", EXIT_USAGE)
    raise _unknown_id(plan, sid, list(_segment_index(plan)), "片段")


def _apply_action(plan: Plan, ids: Sequence[str], action: str, output_id: str | None) -> tuple[list[dict], list[str], list[Output]]:
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    touched: dict[str, Output] = {}
    multi = len(plan.outputs) > 1
    for sid in ids:
        o, s = _resolve_segment(plan, sid, output_id)
        if s.action == action and s.confidence >= 1.0:
            unchanged.append(sid)  # 已经是人工确认过的同一个决定：幂等，不保存
            continue
        was = s.action
        plan.set_action(sid, action, o.id)
        item: dict[str, Any] = {"id": sid, "action": action, "was": was}
        if was == action:
            item["confirmed"] = True  # 动作没变，只是把"拿不准"确认掉
        if multi:
            item["output"] = o.id
        changed.append(item)
        touched[o.id] = o
    return changed, unchanged, list(touched.values())


_RANGE_RE = re.compile(r"\[(-?[\d.]+), (-?[\d.]+|inf)\]")


def _apply_boundary(plan: Plan, sid: str, start: float | None, end: float | None, output_id: str | None) -> tuple[list[dict], list[str], list[Output]]:
    o, s = _resolve_segment(plan, sid, output_id)
    before = {x.id: (x.start, x.end) for x in o.segments}
    edits = [("start", start), ("end", end)]
    if start is not None and end is not None and start >= s.end:
        edits.reverse()  # 整段往后挪：先挪终点，起点才有地方去
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for edge, t in edits:
        if t is None:
            continue
        old = getattr(s, edge)
        if abs(old - t) < 1e-4:
            unchanged.append(f"{sid}.{edge}")
            continue
        try:
            plan.move_boundary(sid, edge, t, o.id)
        except PlanError as e:
            m = _RANGE_RE.search(str(e))
            if not m:
                raise CliError("boundary_out_of_range", str(e), "换一个在允许范围内的值", EXIT_USAGE) from None
            lo, hi = float(m.group(1)), float(m.group(2))
            clamp = min(max(t, lo), hi)
            raise CliError("boundary_out_of_range", f"{sid} 的 {edge} 只能在 {lo:.2f}–{hi:.2f} 之间（收到 {t:g}）",
                           f"换一个范围内的值，例如 --{edge} {clamp:.2f}；要越过邻居，先把邻居改短", EXIT_USAGE) from None
        changed.append({"id": sid, "edge": edge, "to": _r(getattr(s, edge), 3), "was": _r(old, 3)})
    moved = [x.id for x in o.segments if x.id != sid and before[x.id] != (x.start, x.end)]
    if moved and changed:
        changed[-1]["moved"] = moved
    return changed, unchanged, [o] if changed else []


def _apply_select(plan: Plan, ids: Sequence[str], value: bool) -> tuple[list[dict], list[str]]:
    out_ids = [o.id for o in plan.outputs]
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for oid in ids:
        if oid not in out_ids:
            if oid in _segment_index(plan):
                raise CliError("usage", f"{oid} 是片段 id，--select/--deselect 只对输出 id 生效",
                               f"可选的输出：{'、'.join(out_ids)}；片段用 --keep / --cut", EXIT_USAGE)
            raise _unknown_id(plan, oid, out_ids, "输出")
        o = plan.output(oid)
        if o.selected == value:
            unchanged.append(oid)
            continue
        changed.append({"id": oid, "selected": value, "was": o.selected})
        o.selected = value
    return changed, unchanged


# ================================================================== 命令：review / render / export / check


def cmd_review(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    plan = _load_plan(args.plan)
    P = _q(_rel(plan.path))
    rev0 = plan.revision
    if args.dry_run:
        where = f"127.0.0.1:{args.port}" if args.port else "127.0.0.1 的空闲端口"
        how = "在后台" if args.detach else "在前台"
        return {"plan": _rel(plan.path), "revision": rev0, "dry_run": True,
                "would": f"{how}在 {where} 启动审阅页" + ("" if args.no_open else "并打开浏览器"),
                "notes": [], "next": [_nx(_rerun_without(ui.argv, "--dry-run"), "启动审阅页")]}
    if args.detach:
        return _review_detached(plan, args)
    if not _isatty(sys.stdout):
        raise CliError("would_block", "review 会一直运行（本地审阅页服务器，直到 Ctrl+C），在这里运行会把调用方卡住",
                       f"这个命令会一直运行。把 `autocut review {P}` 交给用户自己运行；要替用户打开就加 --detach"
                       "（后台启动，立即返回网址和 pid，用完 kill <pid>）", EXIT_USAGE)
    from autocut.review.server import serve

    log.info("review：%s（rev %d），port=%d，open=%s", plan.path, rev0, args.port, not args.no_open)
    serve(plan.path, port=args.port, open_browser=not args.no_open)
    rev1 = _disk_revision(plan.path)
    nxt = [_nx(f"autocut show {P}", "看改完的摘要"), _nx(f"autocut render {P}", "出片")]
    return {"plan": _rel(plan.path), "revision": rev1, "revision_before": rev0, "changed": rev1 != rev0, "notes": [], "next": nxt}


def _review_detached(plan: Plan, args: argparse.Namespace) -> dict[str, Any]:
    """后台启动审阅页（独立进程组，调用方退出也不受影响），等它打出网址就返回。"""
    P = _q(_rel(plan.path))
    log_path = Path(tempfile.gettempdir()) / f"autocut-review-{os.getpid()}-{int(time.time())}.log"
    with open(log_path, "w", encoding="utf-8") as logf:
        child = subprocess.Popen([sys.executable, "-c", _REVIEW_CHILD, str(plan.path), str(args.port), "0" if args.no_open else "1"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=logf, start_new_session=True, close_fds=True)
    url = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        m = re.search(r"http://127\.0\.0\.1:\d+/", log_path.read_text(encoding="utf-8", errors="replace"))
        if m:
            url = m.group(0)
            break
        if child.poll() is not None:
            break
        time.sleep(0.05)
    if url is None:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGTERM)
        tail = " ".join(log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:])
        raise CliError("review_failed", f"审阅页没有启动起来：{tail or '没有输出'}",
                       f"看日志 {log_path}；端口被占用就换 --port 或用默认的 0", EXIT_RUNTIME)
    log.info("review --detach：%s pid=%d（日志 %s）", url, child.pid, log_path)
    return {"plan": _rel(plan.path), "revision": plan.revision, "url": url, "pid": child.pid, "stop": f"kill {child.pid}",
            "log": str(log_path), "tell_user": f"审阅页已经在后台打开：{url}。逐条看完、改完告诉我；用完我会把它关掉。",
            "notes": [], "next": [_nx(f"autocut show {P}", "用户改完后重新读方案（revision 会变）"),
                                  _nx(f"kill {child.pid}", "用完关掉审阅页")]}


_STAGES = {"prepare": "准备", "measure": "测响度", "encode": "编码", "verify": "核验", "done": "完成"}


def cmd_render(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    plan = _load_plan(args.plan)
    _need_ffmpeg()
    P = _q(_rel(plan.path))
    outs = [_pick_output(plan, i) for i in args.output] if args.output else [o for o in plan.outputs if o.selected]
    if not outs:
        first = plan.outputs[0].id
        raise CliError("nothing_selected", "方案里没有被选中的输出，不知道出哪条",
                       f"先选一条：autocut set {P} {first} --select；或者 autocut render {P} --output {first}", EXIT_INPUT)
    _check_sources(plan, outs)
    from autocut.render import default_stem, render

    base = Path(args.out_dir) if args.out_dir else plan.path.parent
    stem = default_stem(plan)
    targets = {o.id: base / f"{stem}.{o.id}.mp4" for o in outs}
    existing = [p for p in targets.values() if p.exists()]
    if existing and not args.force and not args.dry_run:
        raise CliError("exists", f"成片已存在：{'、'.join(_rel(p) or '' for p in existing)}",
                       "加 --force 覆盖，或者 --out-dir 换个目录", EXIT_INPUT)
    state = {"i": -1, "stage": ""}

    def progress(stage: str, frac: float) -> None:
        if stage == "prepare" and frac == 0.0:
            state["i"] += 1
        i = max(state["i"], 0)
        label = f"出片 {outs[min(i, len(outs) - 1)].id}" + (f"（{i + 1}/{len(outs)}）" if len(outs) > 1 else "") + f" · {_STAGES.get(stage, stage)}"
        ui.activity.update(label, frac if stage == "encode" else None)
        if stage != state["stage"]:
            state["stage"] = stage
            log.info(label)

    log.info("render：%s 输出 %s → %s（force=%s dry_run=%s verify=%s）", plan.path.name, [o.id for o in outs], base, args.force,
             args.dry_run, not args.no_verify)
    t0 = time.perf_counter()
    wall0 = time.time()
    verifies: dict[str, dict[str, Any]] = {}
    try:
        with _cancellable("render") if not args.dry_run else contextlib.nullcontext(), ui.activity.spin(f"出片 {outs[0].id}"):
            results = render(plan, output_ids=[o.id for o in outs], out_dir=args.out_dir, burn_captions=args.burn_captions,
                             loudnorm=args.loudnorm, progress=progress, dry_run=args.dry_run)
            if not args.dry_run and not args.no_verify:
                for r in results:
                    ui.activity.update(f"核验 {r.output_id}")
                    verifies[r.output_id] = _verify_render(plan, plan.output(r.output_id), r, P)
    except (_Cancelled, KeyboardInterrupt):
        removed = _remove_partial_renders(base, stem, outs, wall0)
        log.warning("render 已取消：ffmpeg 已停，.part.mp4 由 render 自己删掉；兜底又删了 %s",
                    [_rel(p) for p in removed] or "0 个文件（没有剩下的）")
        raise
    seconds = time.perf_counter() - t0
    rows, notes = [], []
    for r in results:
        row: dict[str, Any] = {"output": r.output_id, "path": _rel(r.path), "duration": _r(r.duration, 3),
                               "expected": _r(r.expected_duration, 3), "av_drift": _r(r.av_drift, 3), "srt": _rel(r.srt_path),
                               "loudness": None, "warnings": list(r.warnings)}
        if r.loudness:
            row["loudness"] = {"target": r.loudness.get("target"), "before": _r(float(r.loudness["input_i"]), 1)}
        v = verifies.get(r.output_id)
        if v is not None:
            if row["loudness"] is not None and v.get("lufs") is not None:
                row["loudness"]["lufs"] = v["lufs"]
            row["warnings"] += [f"核验：{x}" for x in v.get("problems", [])]
            row["verify"] = v
        if args.dry_run:
            row.update(_dry_run_command(r.command))
            row["exists"] = r.path.exists()
        rows.append(row)
        notes.extend(f"{r.output_id}：{w}" for w in row["warnings"] if not args.dry_run)
    if args.dry_run and existing and not args.force:
        notes.append(f"{len(existing)} 个成片已存在：真正出片时要加 --force 才会覆盖")
    if args.no_verify and not args.dry_run:
        notes.append("跳过了核验（--no-verify）：时长、音画同步、接缝都没有独立检查过，转述时不要说\"已核验\"")
    payload: dict[str, Any] = {"plan": _rel(plan.path), "results": rows, "seconds": _r(seconds, 1)}
    if args.dry_run:
        payload["dry_run"] = True
        nxt = [_nx(_rerun_without(ui.argv, "--dry-run"), "真正出片")]
    else:
        bad = [r for r in rows if r.get("verify") and not r["verify"]["verified"]]
        if bad:
            payload["notice"] = f"{len(bad)}/{len(rows)} 条没通过核验（见 warnings）；成片已经写出，转述时要说明"
        mp4 = _q(rows[0]["path"])
        nxt = [_nx(f"autocut check {mp4}", "发布前体检（留存风险信号，不是爆款保证）"), _nx(f"open {mp4}", "看一眼成片")]
        mids = (bad[0]["verify"].get("mid_speech_at") or []) if bad else []
        if mids:
            nxt.insert(0, _nx(f"autocut show {P} --around {_mmss(mids[0], True)}", "看落在说话中间的接缝是哪一刀"))
    return {**payload, "notes": notes, "next": nxt}


def _remove_partial_renders(base: Path, stem: str, outs: Sequence[Output], since: float) -> list[Path]:
    """取消时清理：render 自己会删 .part.mp4 和临时目录；这里兜底，并删掉这次写了 .srt 但成片没写完的那几条的 .srt。

    只删按名字算出来的确切路径（render 用的 .<名>.<输出id>.part.mp4 和 <名>.<输出id>.srt），而且只删这次运行写的
    （修改时间不早于开始时间）；已经写完的成片、之前就有的文件都不动。"""
    removed: list[Path] = []
    for o in outs:
        mp4, srt, part = base / f"{stem}.{o.id}.mp4", base / f"{stem}.{o.id}.srt", base / f".{stem}.{o.id}.part.mp4"
        finished = mp4.exists() and mp4.stat().st_mtime >= since
        for p in [part] + ([] if finished else [srt]):
            if p.exists() and p.stat().st_mtime >= since:
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
                    removed.append(p)
    return removed


def _stream_durations(path: Path) -> tuple[float, float | None, float | None]:
    """(容器时长, 视频流时长, 音频流时长)：核验自己量一遍，不用 render 的测量结果。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout or "{}")
    fmt = float((d.get("format") or {}).get("duration") or 0.0)
    got: dict[str, float | None] = {"video": None, "audio": None}
    for s in d.get("streams", []):
        k = s.get("codec_type")
        if k in got and got[k] is None and s.get("duration") not in (None, "N/A"):
            got[k] = float(s["duration"])
    return fmt, got["video"], got["audio"]


def _joints(o: Output) -> list[tuple[float, bool]]:
    """接缝：成片里前后两个保留片段在素材里不连续的位置 → [(成片时间, 听不听得见)]。两边都静音的接缝只在画面上。"""
    tl = o.timeline()
    out = []
    for (a, _, _), (b, b0, _) in zip(tl, tl[1:]):
        contiguous = a.source == b.source and abs(a.end - b.start) < 1e-3 and a.slot is None and b.slot is None and a.speed == b.speed
        if not contiguous:
            out.append((b0, not (a.mute and b.mute)))
    return out


def _verify_render(plan: Plan, o: Output, r: Any, plan_arg: str) -> dict[str, Any]:
    """出片后独立核验：重新量时长和音画漂移、测响度，并在成片上重新检测语音，看接缝有没有落在说话中间。"""
    from autocut.analysis import Media
    from autocut.loudness import integrated_lufs
    from autocut.probe import probe

    t0 = time.perf_counter()
    info = probe(r.path)
    fps = info.video.fps if info.video and info.video.fps > 0 else 30.0
    frame = 1.0 / fps
    fmt, vd, ad = _stream_durations(r.path)
    problems: list[str] = []
    expected = o.duration
    if abs(fmt - expected) > frame + 1e-3:
        problems.append(f"时长 {fmt:.3f}s 和方案的 {expected:.3f}s 差 {abs(fmt - expected):.3f}s（超过 1 帧）")
    drift = abs(vd - ad) if vd is not None and ad is not None else None
    if drift is not None and drift > frame + 1e-3:
        problems.append(f"音画时长差 {drift:.3f}s（超过 1 帧）：视频 {vd:.3f}s，音频 {ad:.3f}s")
    joints = _joints(o)
    audible = [t for t, heard in joints if heard]
    v: dict[str, Any] = {"verified": True, "duration": _r(fmt, 3), "av_drift": _r(drift, 3), "joints": len(joints)}
    if not info.audio:
        v["joints_mid_speech"] = None
        v["mid_speech_skipped"] = "成片没有音轨"
    elif any(b.role == "music" for b in o.beds) and audible:
        v["joints_mid_speech"] = None
        v["mid_speech_skipped"] = "成片里有 BGM，语音检测分不开人声和音乐，没有检查接缝"
    elif any(b.role == "narration" for b in o.beds) and audible:
        v["joints_mid_speech"] = None
        v["mid_speech_skipped"] = "成片里有配音解说，语音检测分不开原声和解说（解说在接缝处连续，不会被剪断），没有检查接缝；原声的接缝要在加解说之前核验"
    else:
        regions = Media(r.path).speech().regions if audible else ()
        mid = [t for t in audible if any(s + JOINT_MARGIN < t < e - JOINT_MARGIN for s, e in regions)]
        v["joints_mid_speech"] = len(mid)
        if mid:
            v["mid_speech_at"] = [_r(t) for t in mid[:UNCERTAIN_LIMIT]]
            problems.append(f"{len(mid)} 个接缝落在说话中间（成片 {'、'.join(_mmss(t, True) for t in mid[:5])}）："
                            f"用 autocut show {plan_arg} --around {_mmss(mid[0], True)} 看是哪一刀")
    target = r.loudness.get("target") if r.loudness else None
    if target is not None:
        lufs = integrated_lufs(r.path)
        v["lufs"] = _r(lufs, 1)
        if not abs(lufs - target) <= LOUDNESS_TOL:
            problems.append(f"响度 {lufs:.1f} LUFS，和目标 {target:g} 差超过 {LOUDNESS_TOL:g} LU")
    if problems:
        v["verified"] = False
        v["problems"] = problems
    log.info("核验 %s：%s，%d 个接缝（%s 个落在说话中间），%.2fs", o.id, "通过" if not problems else problems,
             len(joints), v["joints_mid_speech"], time.perf_counter() - t0)
    return v


def _dry_run_command(cmd: list[str]) -> dict[str, Any]:
    """dry-run：完整命令写进滤镜图旁边的 command.sh，JSON 里只放截短的命令和两个文件路径。"""
    full = shlex.join(cmd)
    out: dict[str, Any] = {"command": _trunc(full, 400)}
    if "-/filter_complex" in cmd:
        graph = Path(cmd[cmd.index("-/filter_complex") + 1])
        sh = graph.with_name("command.sh")
        sh.write_text(full + "\n", encoding="utf-8")
        out["filter_graph"] = str(graph)
        out["command_file"] = str(sh)
    return out


def cmd_export(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    plan = _load_plan(args.plan)
    _need_ffmpeg()
    o = _pick_output(plan, args.output)
    from autocut import export as ex

    path = Path(args.out) if args.out else ex._out_path(plan, o, args.format, None)
    if path.exists() and not args.force and not args.dry_run:
        raise CliError("exists", f"{_rel(path)} 已存在", "加 --force 覆盖，或者 -o 写到别的路径", EXIT_INPUT)
    _check_sources(plan, [o])
    limits = _export_limits(o, args.format)
    payload: dict[str, Any] = {"plan": _rel(plan.path), "path": _rel(path), "format": args.format, "output": o.id, "limits": limits}
    if args.dry_run:
        payload.update(dry_run=True, exists=path.exists())
        notes = ["文件已存在：真正导出时要加 --force 才会覆盖"] if path.exists() and not args.force else []
        return {**payload, "notes": notes, "next": [_nx(_rerun_without(ui.argv, "--dry-run"), "真正导出")]}
    log.info("export：%s %s → %s（cuts=%s）", o.id, args.format, path, args.cuts)
    try:
        written = ex.export(plan, args.format, path, o.id, cuts=args.cuts)
    except ValueError as e:
        raise CliError("no_transcript" if args.format == "srt" else "bad_option", str(e),
                       "先用 autocut doctor 装好语音识别再重跑配方；或者先导出 fcpxml" if args.format == "srt" else "看 autocut export --help",
                       EXIT_INPUT) from None
    payload["path"] = _rel(written)
    checks = _selfcheck_export(Path(written), args.format, o)
    if checks["problems"]:
        raise CliError("export_invalid", f"导出的 {_rel(written)} 没通过结构自检：{'；'.join(checks['problems'])}",
                       "这是 autocut 的问题：把方案和这条输出报告给开发者；先换个格式导出（例如 --format edl）", EXIT_RUNTIME)
    payload["selfcheck"] = checks["passed"]
    payload["verified_in_nle"] = False
    what = "字幕文件" if args.format == "srt" else f"{EXPORT_TARGETS[args.format].split('（')[0]} 的工程"
    payload["tell_user"] = f"已生成{what}（{_rel(written)}），结构已自检，还没在剪辑软件里打开过"
    how = {"fcpxml": "Final Cut Pro 双击打开；DaVinci Resolve 用 文件 → 导入 → 时间线", "markers": "Final Cut Pro 双击打开（只有标记）",
           "xmeml": "Premiere Pro：文件 → 导入", "edl": "导入剪辑软件（只有主轨剪辑点）", "srt": "上传到平台，或导入剪辑软件的字幕轨"}
    return {**payload, "notes": [], "next": [_nx(f"open {_q(_rel(written))}", how[args.format])]}


def _fcp_seconds(v: str | None) -> float | None:
    """FCPXML 的有理数时间 "67/2s" / "8s" → 秒。"""
    if not v or not v.endswith("s"):
        return None
    num, _, den = v[:-1].partition("/")
    try:
        return float(num) / float(den or 1)
    except ValueError:
        return None


def _selfcheck_export(path: Path, fmt: str, o: Output) -> dict[str, list[str]]:
    """导出文件的结构自检：能解析、时间线总长和方案一致（差不超过 1 帧）、引用的素材文件都在。不等于在剪辑软件里打开过。"""
    import xml.etree.ElementTree as ET
    from urllib.parse import unquote, urlparse

    passed: list[str] = []
    problems: list[str] = []
    tol = 1 / 24 + 1e-3
    expected = o.duration

    def files_exist(urls: list[str]) -> None:
        missing = [u for u in urls if not Path(unquote(urlparse(u).path)).exists()]
        if missing:
            problems.append(f"引用的素材不存在：{missing[0]}")
        else:
            passed.append(f"{len(urls)} 个素材路径都存在")

    if fmt in ("fcpxml", "markers", "xmeml"):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as e:
            return {"passed": passed, "problems": [f"XML 解析失败：{e}"]}
        passed.append("XML 能解析")
        if fmt == "xmeml":
            seq = root.find("sequence")
            dur, tb = (seq.findtext("duration"), seq.findtext("rate/timebase")) if seq is not None else (None, None)
            total = int(dur) / int(tb) if dur and tb else None
            files_exist([e.text or "" for e in root.iter("pathurl")])
        else:
            seq = root.find(".//sequence")
            total = _fcp_seconds(seq.get("duration")) if seq is not None else None
            files_exist([e.get("src", "") for e in root.iter("media-rep")])
        if root.tag != ("xmeml" if fmt == "xmeml" else "fcpxml"):
            problems.append(f"根元素是 <{root.tag}>")
        if total is None:
            problems.append("找不到时间线（sequence）的时长")
        elif fmt != "markers" and abs(total - expected) > tol:
            problems.append(f"时间线 {total:.3f}s 和方案的 {expected:.3f}s 不一致")
        else:
            passed.append(f"时间线 {total:.2f}s 和方案一致")
    elif fmt == "edl":
        lines = path.read_text(encoding="utf-8").splitlines()
        events = [ln for ln in lines if re.match(r"^\d{3}\s", ln)]
        if not lines or not lines[0].startswith("TITLE:"):
            problems.append("第一行不是 TITLE:")
        if len(events) != len(o.kept()):
            problems.append(f"剪辑点 {len(events)} 个，方案里保留的片段是 {len(o.kept())} 个")
        else:
            passed.append(f"{len(events)} 个剪辑点和方案一致")
        files_exist(["file://" + ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("* SOURCE FILE:")])
    elif fmt == "srt":
        from autocut.captions import parse_srt

        cues = parse_srt(path.read_text(encoding="utf-8"))
        if not cues:
            problems.append("一条字幕都没有")
        elif any(c.end < c.start for c in cues) or any(b.start < a.start for a, b in zip(cues, cues[1:])):
            problems.append("字幕时间倒序")
        elif cues[-1].end > expected + 0.5:
            problems.append(f"最后一条字幕结束在 {cues[-1].end:.2f}s，超过了成片的 {expected:.2f}s")
        else:
            passed.append(f"{len(cues)} 条字幕，时间都在成片之内")
    log.info("导出自检 %s：通过 %s，问题 %s", path.name, passed, problems)
    return {"passed": passed, "problems": problems}


def _export_limits(o: Output, fmt: str) -> list[str]:
    """这个格式带不过去的东西：要在剪辑软件里补。"""
    if fmt == "srt":
        return []
    lim: list[str] = []
    if o.overlays:
        lim.append(f"{len(o.overlays)} 个叠加层（图片/文字）不导出，需要在剪辑软件里重做")
    if fmt == "edl" and o.beds:
        lim.append("EDL 只描述主轨剪辑点：BGM 和配音不在里面")
    elif any(b.duck_db for b in o.beds):
        lim.append("BGM 的自动压低（ducking）不导出：在剪辑软件里用自动闪避或手动压")
    if any(s.pitch for s in o.segments):
        lim.append("变调（pitch）不导出：在剪辑软件里加变调效果")
    if o.reframe is not None:
        lim.append("改画幅的裁切框（reframe）不导出：在剪辑软件里调画面位置")
    if o.captions.enabled and o.captions.burn:
        lim.append("烧录字幕不在工程里：另外 export --format srt 导入字幕")
    if any(s.slot is not None and s.slot > s.src_duration / s.speed + 0.05 for s in o.kept()):
        lim.append("镜头不够长、渲染时定格补齐的地方，工程里要手动加冻结帧")
    return lim


def cmd_check(args: argparse.Namespace, ui: Ui) -> dict[str, Any]:
    target = Path(args.target)
    if not target.exists():
        raise CliError("not_found", f"找不到要体检的文件 {args.target}（当前目录 {Path.cwd()}）",
                       "给成片（mp4）或方案（.autocut.json）的路径", EXIT_INPUT)
    _need_ffmpeg()
    from autocut.check import check

    tgt: Plan | Path
    if target.suffix == ".json":
        plan = _load_plan(target)
        o = _pick_output(plan, args.output)
        _check_sources(plan, [o])
        tgt, oid = plan, o.id
    else:
        if args.output:
            raise CliError("bad_option", "--output 只对方案（.json）有效", "体检成片时去掉 --output", EXIT_USAGE)
        tgt, oid = target, None
    log.info("check：%s platform=%s output=%s asr=%s", target, args.platform, oid, not args.no_asr)
    with ui.activity.spin(f"体检 {target.name}"):
        rep = check(tgt, platform=args.platform, output_id=oid, asr=not args.no_asr)
    items = sorted(rep.items, key=lambda i: (SEVERITY_ORDER.get(i.severity, 3), i.at or 0.0))
    metrics = {k: v for k, v in rep.metrics.items() if k not in ("dead_air", "platform_guessed")}
    payload: dict[str, Any] = {"target": _rel(target) + (f"#{oid}" if oid else ""), "mode": rep.mode, "platform": rep.platform,
                               "platform_guessed": bool(rep.metrics.get("platform_guessed")), "duration": _r(rep.duration),
                               "items": [i.to_dict() for i in items[:CHECK_ITEMS_LIMIT]], "items_total": len(items),
                               "summary": rep.summary, "metrics": metrics}
    if args.save:
        payload["saved"] = _rel(rep.save(args.save))
    nxt: list[dict[str, str]] = []
    risky = any(i.severity == "risk" for i in items)
    if rep.mode == "plan" and risky:
        nxt.append(_nx(f"autocut review {_q(_rel(target))}", "按修法在审阅页里改（改完再体检一次）"))
    if not args.save:
        nxt.append(_nx(f"autocut check {_q(_rel(target))} --save {_q(target.stem + '.check.json')}", "把报告存档，发布后对照留存曲线"))
    if not items:
        payload["notice"] = "没有发现明显的留存风险信号；这不是爆款保证"
    elif len(items) > CHECK_ITEMS_LIMIT:
        payload["notice"] = f"只列了 {CHECK_ITEMS_LIMIT}/{len(items)} 项（风险优先）"
    return {**payload, "notes": [], "next": nxt}


COMMANDS: dict[str, Callable[[argparse.Namespace, Ui], dict[str, Any]]] = {
    "doctor": cmd_doctor, "transcribe": cmd_transcribe, "transcript": cmd_transcript, "clip": cmd_clip, "analyze": cmd_analyze, "cut": cmd_cut, "highlights": cmd_highlights, "montage": cmd_montage,
    "ytpmv": cmd_ytpmv, "narrate": cmd_narrate, "show": cmd_show, "set": cmd_set, "review": cmd_review,
    "render": cmd_render, "export": cmd_export, "check": cmd_check,
}


# ================================================================== 给人看的输出（只读 JSON 结果，保证两种输出说的是同一件事）

_LABEL_W = 7
_IND = " " * (2 + _LABEL_W)  # 续行和 "✓ 方案   " 后面的内容对齐


def _label(st: Style, kind: str, icon: str, name: str, w: int = _LABEL_W) -> str:
    return st(kind, icon) + " " + st("bold", _pad(name, w) if _width(name) < w else name + " ")


def _next_lines(st: Style, nxt: Sequence[dict[str, str]]) -> list[str]:
    if not nxt:
        return []
    w = max(_width(n["cmd"]) for n in nxt)
    return [st("bold", "下一步")] + [f"  {st('accent', _pad(n['cmd'], w))}   {st('dim', n['why'])}" for n in nxt]


def _notes_lines(st: Style, notes: Sequence[str]) -> list[str]:
    # notes 里既有警告也有说明，用中性的 "·"；真正的降级/警告在各自那一行已经用 ⚠ 标出
    return [f"{st('accent', '·')} {n}" for n in notes]


def _tail(st: Style, res: dict[str, Any]) -> list[str]:
    """每个命令给人看的输出结尾：notice（空结果、不够数）→ notes → 下一步。"""
    head = [f"{st('warn', '›')} {st('bold', res['notice'])}"] if res.get("notice") else []
    return head + _notes_lines(st, res.get("notes", [])) + _next_lines(st, res.get("next", []))


def _reason(r: str) -> str:
    return REASONS.get(r, r)


def _plan_line(st: Style, res: dict[str, Any], extra: str = "") -> str:
    if res.get("dry_run"):
        return _label(st, "dim", "○", "方案") + f"{res['plan']}（--dry-run，没写文件）{extra}"
    return _label(st, "ok", "✓", "方案") + f"{res['plan']}" + st("dim", f"（rev {res['revision']}）") + extra


def _uncertain_lines(st: Style, summ: dict[str, Any], limit: int = 5) -> list[str]:
    lines = []
    for u in summ.get("uncertain", [])[:limit]:
        act = "剪" if u["action"] == "cut" else "留"
        txt = f"「{_trunc(u['text'], 20)}」" if u.get("text") else ""
        lines.append(f"{_IND}  {st('dim', _mmss(u['at'], True))}  {u['id']}  {act} · {_reason(u['reason'])}  "
                     f"{st('warn', format(u['confidence'], '.2f'))}  {_trunc(u['detail'], 40)}{txt}")
    more = summ.get("uncertain_total", 0) - len(lines)
    if more > 0:
        lines.append(f"{_IND}  {st('dim', f'……还有 {more} 处，用 show --filter uncertain 看全部')}")
    return lines


def _human_summary(st: Style, recipe: str, summ: dict[str, Any]) -> list[str]:
    """配方摘要的主体（cut / show 共用），不含"✓ 方案"那一行。"""
    lines: list[str] = []
    if "candidates" in summ:
        for c in summ["candidates"]:
            if c.get("bait"):
                icon, kind, tail = "✗", "err", "诱饵，没选"
            elif c.get("weak") and not c["selected"]:
                icon, kind, tail = "△", "warn", "弱候选，没选（确认可用再 set --select）"
            elif c["selected"]:
                icon, kind, tail = "●", "ok", ""
            else:
                icon, kind, tail = "○", "dim", "没选"
            head = f"{st(kind, icon)} {_pad(c['id'], 8)} {_mmss(c['start'])}–{_mmss(c['end'])}  {c['duration']:.0f}s  分 {c['score']}"
            lines.append(f"  {head}  {c['title']}" + (st("dim", f"  {tail}") if tail else ""))
            reasons = [r for r in c["reasons"] if "没选" not in r]
            if reasons:
                lines.append(f"    {_pad('', 9)}{st('dim', ' · '.join(_trunc(r, 30) for r in reasons[:3]))}")
        more = summ["candidates_total"] - len(summ["candidates"])
        if more > 0:
            lines.append(f"  {st('dim', f'……还有 {more} 个候选（show --output all）')}")
        return lines
    if recipe == "cut" or "removed" in summ:
        span, dur, rem = summ.get("source_span") or 0, summ["duration"], summ.get("removed") or 0
        pct = f"，{rem / span:.0%}" if span else ""
        lines.append(f"{_IND}{_mmss(span)} → {st('bold', _mmss(dur))}（剪掉 {_mmss(rem)}{pct}）")
        counts = " · ".join(f"{_reason(k)} {v}" for k, v in sorted(summ.get("cut", {}).items(), key=lambda kv: -kv[1]))
        unc = summ.get("uncertain_total", 0)
        lines.append(f"{_IND}{st('cut', '✂')} {counts or '没有剪'}" + (f"      {st('warn', f'⚠ 拿不准 {unc}')}" if unc else ""))
        return lines + _uncertain_lines(st, summ)
    if recipe == "montage":
        lines.append(f"{_IND}{summ['bpm']:g} BPM · {summ['bars']} 小节 · {summ['duration']:.1f}s · {summ['shots_total']} 个镜头 · "
                     f"用到 {len(summ['sources_used'])}/{summ['sources_total']} 个素材")
        lines.append(f"{_IND}{st('dim', ' · '.join(f'{k} ×{v}' for k, v in summ['sources_used'].items()))}")
        return lines
    if recipe == "ytpmv":
        frm = "文字稿" if summ["syllables_from"] == "words" else "声音能量（没有文字）"
        lines.append(f"{_IND}{summ['bpm']:g} BPM · {summ['bars']} 小节 · {summ['duration']:.1f}s · {summ['placements']} 次放置"
                     f"（{summ['distinct']} 个不同音节）")
        lines.append(f"{_IND}{summ['syllables']} 个音节，按{frm}切" + (f"：{st('accent', summ['syllable_text'])}" if summ["syllable_text"] else ""))
        return lines
    if recipe == "narrate":
        lines.append(f"{_IND}配音 {summ['narration']:.1f}s · {summ['sentences']} 句 · {summ['voice']} · {summ['size']}"
                     + (" · 有 BGM" if summ.get("music") else ""))
        if summ.get("hook"):
            lines.append(f"{_IND}开头钩子「{summ['hook']['text']}」{summ['hook']['duration']:.1f}s")
        for im in summ["images"]:
            sent = im.get("sentences")
            sents = f"第 {sent[0]}–{sent[1]} 句" if sent and sent[0] != sent[1] else (f"第 {sent[0]} 句" if sent else "")
            lines.append(f"{_IND}  {_pad(im['source'], 10)} {_mmss(im['at'])} 起 {im['hold']:.1f}s  {st('dim', sents)}")
        return lines
    # 通用（高光的某一条、手工方案）
    title = f"  {summ['title']}" if summ.get("title") else ""
    lines.append(f"{_IND}{_mmss(summ.get('start'))}–{_mmss(summ.get('end'))} → {summ['duration']:.1f}s · {summ['kept']}/{summ['segments']} 段保留"
                 + ("" if summ.get("selected", True) else " · 没选中") + title)
    for r in summ.get("reasons", [])[:4]:
        lines.append(f"{_IND}{st('dim', '· ' + r)}")
    return lines + _uncertain_lines(st, summ)


def h_doctor(res: dict[str, Any], st: Style) -> list[str]:
    ff, asr, tts = res["ffmpeg"], res["asr"], res["tts"]
    w = 10
    ind = " " * (2 + w)
    marks = " · ".join(f"{FFMPEG_FILTERS[k].split('（')[0]} {st('ok', '✓') if ok else st('err', '✗')}" for k, ok in ff["filters"].items())
    voices_ok = len(tts["voices"]) == len(DEFAULT_VOICES)
    lines = [
        (_label(st, "ok", "✓", "ffmpeg", w) + f"{ff['version']}  {st('dim', ff['path'])}" + (f"\n{ind}{marks}" if marks else ""))
        if ff["path"] else _label(st, "err", "✗", "ffmpeg", w) + "没有找到",
        (_label(st, "ok", "✓", "语音识别", w) + f"{asr['model_tag']}  {st('dim', asr['cli'] or '')}") if asr["available"]
        else _label(st, "warn", "⚠", "语音识别", w) + "不可用"
        + st("dim", f"（whisper-cli {'✓' if asr['cli'] else '✗'} · 模型 {'✓' if asr['model'] else '✗'}）"),
        (_label(st, "ok" if voices_ok else "warn", "✓" if voices_ok else "⚠", "配音", w)
         + "say：" + ("、".join(tts["voices"]) or "缺默认声音") + st("dim", f"（共 {tts['voices_total']} 个声音）")) if tts["say"]
        else _label(st, "warn", "⚠", "配音", w) + "没有 say（narrate 不可用）",
    ]
    if res["fix"]:
        lines.append(st("bold", "怎么补") + st("dim", "（只列命令，不会自动下载）"))
        lines += [f"  {_pad(f['for'], 14)} {st('accent', f['cmd'])}  {st('dim', f['download'])}" for f in res["fix"]]
    return lines + _tail(st, res)


def h_transcribe(res: dict[str, Any], st: Style) -> list[str]:
    if res.get("dry_run"):
        head = _label(st, "dim", "○", "文字稿") + f"{res['path']}（--dry-run，没写文件）"
    else:
        head = _label(st, "ok", "✓", "文字稿") + res["path"]
    lines = [head, f"{_IND}{res['words']} 个词 · {res['preview_total']} 句 · {_mmss(res['duration'])} · {res['track']} · "
                   f"模型 {res['model']} · {res['seconds']:.1f}s"]
    lines += [f"{_IND}{st('dim', _mmss(p['at']))}  {p['text']}" for p in res["preview"][:8]]
    if res["preview_total"] > 8:
        lines.append(f"{_IND}{st('dim', f'……共 {res["preview_total"]} 句')}")
    return lines + _tail(st, res)


def h_transcript(res: dict[str, Any], st: Style) -> list[str]:
    lines = [_label(st, "ok", "✓", "文字稿") + f"{res['path']} · 第 {res['offset'] + 1}–{res['offset'] + res['showing']} 句 / 共 {res['total']} 句 · {_mmss(res['duration'])}"]
    lines += [f"{_IND}{st('dim', it['id'])} {st('dim', it['at'])}  {it['text']}" for it in res["items"]]
    return lines + _tail(st, res)


def h_clip(res: dict[str, Any], st: Style) -> list[str]:
    head = _label(st, "ok", "✓", "候选") + f"{res['output']} · {res['title']} · {res['at']} 起 {res['duration']}s（{res['from']}–{res['to']}）"
    return [head, f"{_IND}方案 {res['plan']}（rev {res['revision']}，共 {res['total_candidates']} 条候选）" + ("，已选中" if res["selected"] else "，未选中")] + _tail(st, res)


def h_analyze(res: dict[str, Any], st: Style) -> list[str]:
    if res["kind"] in ("script", "image"):
        head = _label(st, "ok", "✓", "素材") + res["path"] + (f" · 文稿 {res['sentences']} 句" if res["kind"] == "script" else " · 图片")
        lines = [head]
    else:
        parts = [res["path"], _mmss(res["duration"])] + ([res["video"]] if res.get("video") else ["纯音频"])
        lines = [_label(st, "ok", "✓", "素材") + " · ".join(parts)]
        tracks = "、".join(f"{a['track']} {a['title'] or '（无标题）'}" + ("（人声）" if a.get("voice_guess") else "") for a in res["audio"])
        lines.append(f"{_IND}音轨 {tracks or '无'}")
        info = []
        if res.get("speech_ratio") is not None:
            info.append(f"{res['speech_ratio']:.0%} 的时间在说话")
        if res.get("bpm"):
            info.append(f"{res['bpm']:g} BPM")
        if res.get("chat"):
            info.append(f"弹幕 {res['chat']}")
        if info:
            lines.append(_IND + " · ".join(info))
    lines += _notes_lines(st, res["notes"])
    sug = res.get("suggest") or []
    if sug:
        w = max(_width(s["cmd"]) for s in sug)
        lines += [st("bold", "建议")] + [f"  {st('accent', _pad(s['cmd'], w))}   {st('dim', s['why'])}" for s in sug]
    return lines


def h_recipe(res: dict[str, Any], st: Style) -> list[str]:
    summ = res["summary"]
    recipe = res["command"]
    lines: list[str] = []
    for m in res.get("media", []):
        parts = [m["path"], _mmss(m["duration"])] + ([m["video"]] if m.get("video") else []) + (
            [f"音轨 {' / '.join(m['tracks'])}"] if len(m["tracks"]) > 1 else [])
        lines.append(_label(st, "ok", "✓", "素材") + " · ".join(parts))
    if "analysis" in res:
        a = res["analysis"]
        asr = f"文字稿（{a['asr']}）" if a["asr"] else st("warn", "没有文字稿")
        lines.append(_label(st, "warn" if summ.get("degraded") else "ok", "⚠" if summ.get("degraded") else "✓", "分析")
                     + f"{a['track']} · 语音 · {asr} · {a['seconds']:.1f}s")
    extra = ""
    if recipe == "highlights":
        k, n = len(summ["selected"]), summ.get("requested")
        picked = f"选中 {k} 条 / 要求 {n} 条" if n else f"选中 {k} 条"
        extra = f" · {summ['candidates_total']} 个候选，{st('warn', picked) if n and k < n else picked}" + (
            f"，排除 {summ['baits']} 条诱饵" if summ["baits"] else "") + (f"，{len(summ['weak'])} 条弱候选没选" if summ.get("weak") else "")
    lines.append(_plan_line(st, res, extra))
    lines += _human_summary(st, recipe, summ)
    return lines + _tail(st, res)


def h_show(res: dict[str, Any], st: Style) -> list[str]:
    head = f"{res['plan']}" + st("dim", f"（rev {res['revision']} · {res['recipe']}）")
    if res.get("output") == "all":
        lines = [_label(st, "ok", "✓", "方案") + head]
        for o in res["outputs"]:
            mark = st("ok", "●") if o["selected"] else st("dim", "○")
            lines.append(f"  {mark} {_pad(o['id'], 8)} {o['duration']:.1f}s  {o['segments']} 段" + (f"  分 {o['score']}" if o["score"] is not None else "")
                         + (f"  {st('warn', '⚠ ' + str(o['uncertain']))}" if o["uncertain"] else "") + (f"  {o['title']}" if o["title"] else ""))
        return lines + _tail(st, res)
    if "summary" in res:
        lines = [_label(st, "ok", "✓", "方案") + head + (f" · {res['output']}" if res.get("output") else "")]
        return lines + _human_summary(st, res["recipe"], res["summary"]) + _tail(st, res)
    flt = "" if res["filter"] == "all" else f" · {res['filter']}"
    lines = [_label(st, "ok", "✓", "方案") + head + f" · {res['output']}{flt} · {res['showing']}/{res['total']} 条"]
    if res["segments"]:
        lines.append(st("dim", f"  {_pad('id', 6)} {_pad('素材时间', 19)} {_pad('成片', 8)} {_pad('动作', 4)} {_pad('理由', 14)} 把握  说明"))
    else:
        lines.append(st("dim", "  （没有符合条件的片段）"))
    for s in res["segments"]:
        act = st("cut", "剪") if s["action"] == "cut" else st("ok", "留")
        conf = format(s["confidence"], ".2f")
        conf = st("warn", conf) if s["confidence"] < 0.75 else conf
        txt = f" 「{_trunc(s['text'], 24)}」" if s["text"] else ""
        mark = st("accent", " ✎") if s["edited"] else ""
        lines.append(f"  {_pad(s['id'], 6)} {_pad(_mmss(s['start'], True) + '–' + _mmss(s['end'], True), 19)} {_pad(_mmss(s['out_at']), 8)} "
                     f"{act}   {_pad(_reason(s['reason']), 14)} {conf}  {_trunc(s['detail'], 36)}{txt}{mark}")
    return lines + _tail(st, res)


def h_set(res: dict[str, Any], st: Style) -> list[str]:
    rev = res["revision"]
    if not res["changed"]:
        lines = [_label(st, "dim", "·", "没变") + f"{res['plan']}" + st("dim", f"（rev {rev} 不变）")]
        return lines + [f"  {n}" for n in res["notes"]] + _next_lines(st, res["next"])
    if res.get("dry_run"):
        lines = [_label(st, "dim", "○", "预演") + f"{res['plan']}" + st("dim", f"（--dry-run，没保存，rev {rev}）")]
    else:
        lines = [_label(st, "ok", "✓", "已改") + f"{res['plan']}" + st("dim", f"（rev {rev - 1} → {rev}）")]
    word = {"keep": "留", "cut": "剪"}
    for c in res["changed"]:
        if "edge" in c:
            moved = f"（邻居 {'、'.join(c['moved'])} 跟着动）" if c.get("moved") else ""
            lines.append(f"  {c['id']}.{c['edge']}  {c['was']:.3f} → {c['to']:.3f}{st('dim', moved)}")
        elif "selected" in c:
            lines.append(f"  {c['id']}  {'选中' if c['selected'] else '取消选中'}")
        else:
            what = f"确认{word[c['action']]}" if c.get("confirmed") else f"{word[c['was']]} → {word[c['action']]}"
            lines.append(f"  {c['id']}  {what}" + (st("dim", f"（{c['output']}）") if c.get("output") else ""))
    if res.get("duration"):
        b, a = res["duration"]["before"], res["duration"]["after"]
        lines.append(f"  成片 {_mmss(b, True)} → {_mmss(a, True)}（{a - b:+.1f}s）")
    if res.get("selected") is not None:
        lines.append(f"  选中：{'、'.join(res['selected']) or '（无）'}")
    return lines + _tail(st, res)


def h_review(res: dict[str, Any], st: Style) -> list[str]:
    if res.get("url"):
        return [_label(st, "ok", "✓", "审阅页") + f"{st('accent', res['url'])}  {st('dim', f'pid {res["pid"]} · 关掉：{res["stop"]}')}"] + _tail(st, res)
    if res.get("dry_run"):
        return [_label(st, "dim", "○", "审阅") + f"{res['plan']}（rev {res['revision']}）能打开；{res['would']}"] + _next_lines(st, res["next"])
    what = f"rev {res['revision_before']} → {res['revision']}（人改过）" if res["changed"] else f"rev {res['revision']}，没有改动"
    return [_label(st, "ok", "✓", "审阅结束") + f"{res['plan']} · {what}"] + _next_lines(st, res["next"])


def h_render(res: dict[str, Any], st: Style) -> list[str]:
    lines = []
    for r in res["results"]:
        if res.get("dry_run"):
            lines.append(_label(st, "dim", "○", "预演") + f"{r['path']}（方案 {_mmss(r['expected'], True)}）" + (st("warn", " 已存在") if r.get("exists") else ""))
            if r.get("filter_graph"):
                lines.append(f"{_IND}滤镜图 {st('dim', r['filter_graph'])}")
                lines.append(f"{_IND}命令   {st('dim', r['command_file'])}")
            continue
        parts = [_mmss(r["duration"], True) + st("dim", f"（方案 {_mmss(r['expected'], True)}）")]
        if r["av_drift"] is not None:
            parts.append(f"音画漂移 {r['av_drift']:.2f}s")
        if r["loudness"]:
            parts.append(f"响度 {r['loudness']['before']:g} → {r['loudness'].get('lufs', r['loudness']['target']):g} LUFS")
        lines.append(_label(st, "warn" if r["warnings"] else "ok", "⚠" if r["warnings"] else "✓", "成片") + f"{r['path']} · " + " · ".join(parts))
        if r["srt"]:
            lines.append(f"{_IND}字幕 {r['srt']}")
        v = r.get("verify")
        if v is not None:
            mid = v["joints_mid_speech"]
            joints = f"{v['joints']} 个接缝" + ("" if mid is None else f"，{mid} 个落在说话中间") + (f"（{v['mid_speech_skipped']}）" if v.get("mid_speech_skipped") else "")
            mark = st("ok", "✓ 核验通过") if v["verified"] else st("warn", "⚠ 核验没通过")
            lines.append(f"{_IND}{mark} · 时长 · 音画同步" + (" · 响度" if v.get("lufs") is not None else "") + f" · {joints}")
            lines += [f"{_IND}  {st('warn', '·')} {x}" for x in v.get("problems", [])]
    if not res.get("dry_run"):
        lines.append(st("dim", f"用时 {res['seconds']:.1f}s"))
    return lines + _tail(st, res)


def h_export(res: dict[str, Any], st: Style) -> list[str]:
    if res.get("dry_run"):
        lines = [_label(st, "dim", "○", "预演") + f"会写 {res['path']}（{res['format']} · {res['output']}）" + (st("warn", " 已存在") if res["exists"] else "")]
    else:
        lines = [_label(st, "ok", "✓", "导出") + f"{res['path']}" + st("dim", f"（{res['format']} · {res['output']}）")]
        lines.append(f"{_IND}结构自检：{'；'.join(res['selfcheck'])}" + st("warn", " · 还没在剪辑软件里打开过"))
    lines += [f"{_IND}{st('warn', '⚠')} {x}" for x in res["limits"]]
    return lines + _tail(st, res)


def h_check(res: dict[str, Any], st: Style) -> list[str]:
    guess = "（按画幅猜的）" if res["platform_guessed"] else ""
    lines = [_label(st, "ok", "✓", "体检") + f"{res['target']} · {_mmss(res['duration'])} · 平台 {res['platform']}{guess}"
             + st("dim", " · 按方案估算" if res["mode"] == "plan" else "")]
    icon = {"risk": ("err", "✗", "风险"), "warn": ("warn", "⚠", "提醒"), "info": ("dim", "·", "说明")}
    for it in res["items"]:
        kind, ic, name = icon.get(it["severity"], ("dim", "·", it["severity"]))
        at = "" if it["at"] is None else _mmss(it["at"])
        if it["at"] is not None and it["end"] is not None and _mmss(it["end"]) != at:
            at += f"–{_mmss(it['end'])}"
        lines.append(f"{st(kind, ic + ' ' + name)}  {_pad(at, 11)} {it['message']}")
        lines.append(f"{' ' * 8}{_pad('', 11)} {st('dim', '→ ' + it['fix'])}")
    lines.append(st("bold", res["summary"]))
    if res.get("saved"):
        lines.append(st("dim", f"报告已存档：{res['saved']}"))
    return lines + _tail(st, res)


HUMAN: dict[str, Callable[[dict[str, Any], Style], list[str]]] = {
    "doctor": h_doctor, "transcribe": h_transcribe, "transcript": h_transcript, "clip": h_clip, "analyze": h_analyze, "cut": h_recipe, "highlights": h_recipe, "montage": h_recipe, "ytpmv": h_recipe,
    "narrate": h_recipe, "show": h_show, "set": h_set, "review": h_review, "render": h_render, "export": h_export, "check": h_check,
}


# ================================================================== 入口


def _write_json(obj: dict[str, Any]) -> None:
    pretty = _isatty(sys.stdout)  # 人在终端里手动加了 --json：缩进好读；给程序的保持紧凑
    text = json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None, separators=None if pretty else (",", ":"), default=str)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _succeed(ui: Ui, command: str, payload: dict[str, Any]) -> None:
    nxt, notes = payload.pop("next", []), payload.pop("notes", [])
    result = {"ok": True, "command": command, **payload, "next": nxt, "notes": notes}
    if ui.json:
        _write_json(result)
    else:
        sys.stdout.write("\n".join(HUMAN[command](result, ui.style)) + "\n")
        sys.stdout.flush()


def _fail(ui: Ui, err: CliError) -> int:
    if ui.json:
        _write_json({"ok": False, "error": err.to_dict()})
    else:
        ui.activity.clear()
        st = ui.err_style
        sys.stderr.write(f"{st('err', '✗ ' + err.message)}\n  {st('dim', '→ ' + err.fix)}\n")
        sys.stderr.flush()
    return err.exit_code


def _silence_stdout() -> None:
    """下游提前关了管道（例如 `| head`）：把 stdout 指到 /dev/null，免得解释器退出时 flush 再报一次错。"""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(fd, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OK


def _main(argv: Sequence[str] | None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv or not _isatty(sys.stdout)
    err_tty = _isatty(sys.stderr)
    err_style = Style(_color_ok(sys.stderr))
    activity = Activity(err_tty, err_style)
    ui = Ui(json=json_mode, style=Style(not json_mode and _color_ok(sys.stdout)), err_style=err_style, activity=activity, argv=argv)
    try:
        args = build_parser().parse_args(argv)
    except _UsageError as e:
        return _fail(ui, e.to_cli())
    except SystemExit as e:  # --help / --version
        return e.code if isinstance(e.code, int) else 0
    debug = bool(getattr(args, "debug", False))
    with _logging_to_stderr(int(getattr(args, "verbose", 0) or 0), activity, err_style, err_tty):
        log.debug("autocut %s：argv=%s json=%s", __version__, argv, json_mode)
        t0 = time.perf_counter()
        try:
            payload = COMMANDS[args.command](args, ui)
        except BaseException as e:  # noqa: BLE001 —— 任何异常都按输出契约报告，不把 traceback 打到 stdout
            if isinstance(e, SystemExit):
                raise
            err = _classify(e, args.command)
            if debug:
                ui.activity.clear()
                traceback.print_exception(e, file=sys.stderr)
            log.debug("%s 失败（%.2fs）：%s %s", args.command, time.perf_counter() - t0, err.code, err.message)
            return _fail(ui, err)
        log.info("%s 完成，%.2fs", args.command, time.perf_counter() - t0)
        _succeed(ui, args.command, payload)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
