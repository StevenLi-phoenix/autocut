"""生成带真值（ground truth）的合成测试视频。

用 macOS `say` 合成语音，按脚本在已知位置插入停顿 / 口头禅 / 说错重来，
叠加粉噪模拟房间底噪，再配上带时间码的测试画面。每个素材旁边写一份
<name>.truth.json，记录每段语音与停顿的精确区间，供单元测试和 agent eval
做确定性校验。

用法：.venv/bin/python fixtures/make_fixtures.py [--out fixtures/media]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import numpy as np

SR = 48000
log = logging.getLogger("fixtures")

# (kind, payload, tag)：kind=say 时 payload 是文本；kind=gap 时 payload 是秒数。
# tag 标出"理想剪辑"应该怎么处理这段：None=保留，filler=口头禅，retake=被放弃的说错版本。
SCRIPTS: dict[str, dict] = {
    "talk_zh": {
        "voice": "Tingting",
        "noise_dbfs": -52.0,
        "items": [
            ("say", "大家好，欢迎回到我的频道。", None),
            ("gap", 0.35, None),
            ("say", "今天我们来聊一聊，怎么把一段录屏快速剪成可以发布的视频。", None),
            ("gap", 1.9, None),
            ("say", "首先你需要准备好原始素材。", None),
            ("gap", 0.4, None),
            ("say", "嗯", "filler"),
            ("gap", 0.6, None),
            ("say", "然后打开剪辑软件，把素材拖进时间线。", None),
            ("gap", 2.6, None),
            ("say", "第二步是把没用的停顿", "retake"),
            ("gap", 0.7, None),
            ("say", "不对，我重新说一遍。", "retake"),
            ("gap", 0.8, None),
            ("say", "第二步，是把没有用的停顿全部删掉。", None),
            ("gap", 3.4, None),
            ("say", "最后检查一遍，导出就可以了。", None),
            ("gap", 0.3, None),
            ("say", "我们下期再见。", None),
            ("gap", 1.2, None),
        ],
    },
    "lecture_en": {
        "voice": "Samantha",
        "noise_dbfs": -38.0,  # 嘈杂教室：固定 -40dB 阈值会把底噪当成说话
        "items": [
            ("gap", 1.5, None),
            ("say", "Okay, let's get started.", None),
            ("gap", 2.2, None),
            ("say", "Today we cover the gulf of execution and the gulf of evaluation.", None),
            ("gap", 0.45, None),
            ("say", "Um", "filler"),
            ("gap", 0.5, None),
            ("say", "Both ideas come from Don Norman.", None),
            ("gap", 4.0, None),
            ("say", "The gulf of execution is the gap between", "retake"),
            ("gap", 0.9, None),
            ("say", "sorry, let me say that again.", "retake"),
            ("gap", 0.7, None),
            ("say", "The gulf of execution is the gap between what you want and what the interface lets you do.", None),
            ("gap", 0.35, None),
            ("say", "The gulf of evaluation is the gap between what the system shows and what you understand.", None),
            ("gap", 2.8, None),
            ("say", "Questions?", None),
            ("gap", 1.0, None),
        ],
    },
}


def run(cmd: list[str]) -> None:
    log.debug("run: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True)


def synth(text: str, voice: str, tmp: Path) -> np.ndarray:
    """say → 48k mono float32，并裁掉首尾近乎无声的部分，让真值区间贴紧语音。"""
    aiff = tmp / "clip.aiff"
    raw = tmp / "clip.f32"
    run(["say", "-v", voice, "-o", str(aiff), text])
    run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", str(SR), "-f", "f32le", str(raw)])
    x = np.fromfile(raw, dtype=np.float32)
    peak = float(np.max(np.abs(x))) or 1.0
    x = x / peak * 10 ** (-3 / 20)  # 峰值归一到 -3 dBFS
    above = np.flatnonzero(np.abs(x) > 10 ** (-50 / 20))
    if above.size:
        x = x[above[0] : above[-1] + 1]
    return x.astype(np.float32)


def pink_noise(n: int, dbfs: float, seed: int) -> np.ndarray:
    """频域 1/f 整形得到粉噪，按 RMS 缩放到目标 dBFS。"""
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(spec.size)
    f[0] = 1
    spec /= np.sqrt(f)
    y = np.fft.irfft(spec, n)
    y /= np.sqrt(np.mean(y**2)) or 1.0
    return (y * 10 ** (dbfs / 20)).astype(np.float32)


def build(name: str, spec: dict, out: Path, seed: int) -> dict:
    parts: list[np.ndarray] = []
    segments = []
    t = 0.0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for kind, payload, tag in spec["items"]:
            if kind == "say":
                clip = synth(payload, spec["voice"], tmp)
            else:
                clip = np.zeros(int(round(payload * SR)), dtype=np.float32)
            dur = clip.size / SR
            seg = {"kind": "speech" if kind == "say" else "gap", "start": round(t, 4), "end": round(t + dur, 4)}
            if kind == "say":
                seg["text"] = payload
            if tag:
                seg["tag"] = tag
            segments.append(seg)
            parts.append(clip)
            t += dur
        audio = np.concatenate(parts)
        audio = audio + pink_noise(audio.size, spec["noise_dbfs"], seed)
        wav = tmp / "mix.f32"
        audio.astype(np.float32).tofile(wav)
        duration = audio.size / SR
        mp4 = out / f"{name}.mp4"
        vf = (
            "drawtext=fontfile=/System/Library/Fonts/Menlo.ttc:fontsize=36:fontcolor=white:"
            "box=1:boxcolor=black@0.6:x=20:y=20:text='%{pts\\:hms}',"
            f"drawtext=fontfile=/System/Library/Fonts/Menlo.ttc:fontsize=24:fontcolor=white:"
            f"box=1:boxcolor=black@0.6:x=20:y=h-50:text='autocut fixture {name}'"
        )
        run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={duration:.4f}",
            "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(wav),
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(mp4),
        ])
    speech = [s for s in segments if s["kind"] == "speech"]
    truth = {
        "name": name,
        "file": mp4.name,
        "duration": round(duration, 4),
        "fps": 30,
        "sample_rate": SR,
        "noise_dbfs": spec["noise_dbfs"],
        "segments": segments,
        "summary": {
            "speech_total": round(sum(s["end"] - s["start"] for s in speech), 4),
            "speech_kept_total": round(sum(s["end"] - s["start"] for s in speech if "tag" not in s), 4),
            "gap_total": round(sum(s["end"] - s["start"] for s in segments if s["kind"] == "gap"), 4),
            "long_gaps": [s for s in segments if s["kind"] == "gap" and s["end"] - s["start"] >= 1.0],
        },
    }
    (out / f"{name}.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n")
    log.info("%s: %.2fs, %d segments → %s", name, duration, len(segments), mp4)
    return truth


# ---------------------------------------------------------------------------
# 其他视频类型：游戏实况 / 直播切片 / 混剪 / 知识视频 / 鬼畜
# ---------------------------------------------------------------------------

CJK_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
MONO_FONT = "/System/Library/Fonts/Menlo.ttc"


def _t(sec: float) -> np.ndarray:
    return np.arange(int(round(sec * SR))) / SR


def _kick() -> np.ndarray:
    t = _t(0.3)
    f = 50 + 110 * np.exp(-t * 35)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 10)


def _snare(rng: np.random.Generator) -> np.ndarray:
    t = _t(0.2)
    return rng.standard_normal(t.size) * np.exp(-t * 22) * 0.5 + np.sin(2 * np.pi * 180 * t) * np.exp(-t * 30) * 0.4


def _hat(rng: np.random.Generator) -> np.ndarray:
    t = _t(0.06)
    return np.diff(rng.standard_normal(t.size + 1)) * np.exp(-t * 80) * 0.12


def _pluck(freq: float, dur: float) -> np.ndarray:
    t = _t(dur)
    return (np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(4 * np.pi * freq * t)) * np.exp(-t * 4) * 0.45


def _place(buf: np.ndarray, sig: np.ndarray, t: float, gain: float = 1.0) -> None:
    i = int(round(t * SR))
    if i >= buf.size:
        return
    m = min(sig.size, buf.size - i)
    buf[i : i + m] += sig[:m] * gain


def _norm(x: np.ndarray, peak_db: float) -> np.ndarray:
    p = float(np.max(np.abs(x))) or 1.0
    return (x / p * 10 ** (peak_db / 20)).astype(np.float32)


def drum_loop(bpm: float, bars: int, seed: int) -> tuple[np.ndarray, list[float]]:
    """合成一段已知节拍的鼓 + 贝斯循环，返回 (音频, 每拍时间)。底鼓在 1、3 拍，军鼓在 2、4 拍。"""
    rng = np.random.default_rng(seed)
    spb = 60.0 / bpm
    beats = [b * spb for b in range(bars * 4)]
    y = np.zeros(int(round(bars * 4 * spb * SR)), dtype=np.float64)
    kick, snare, hat = _kick(), _snare(rng), _hat(rng)
    roots = [55.0, 43.65, 65.41, 49.0]  # A1 F1 C2 G1
    for b, t in enumerate(beats):
        _place(y, kick if b % 2 == 0 else snare, t)
        _place(y, hat, t)
        _place(y, hat, t + spb / 2, 0.6)
        _place(y, _pluck(roots[(b // 4) % 4], spb * 0.9), t, 0.8)
    return _norm(y, -1.0), [round(t, 6) for t in beats]


def chiptune(sec: float, dbfs_peak: float) -> np.ndarray:
    """方波琶音，模拟持续不断的游戏背景音乐。"""
    notes = [220.0, 261.63, 329.63, 392.0, 329.63, 261.63]
    step = 60 / 140 / 2
    y = np.zeros(int(round(sec * SR)), dtype=np.float64)
    t = 0.0
    k = 0
    while t < sec:
        tt = _t(step)
        _place(y, np.sign(np.sin(2 * np.pi * notes[k % len(notes)] * tt)) * np.exp(-tt * 6) * 0.3, t)
        t += step
        k += 1
    return _norm(y, dbfs_peak)


def explosion(rng: np.random.Generator) -> np.ndarray:
    t = _t(1.0)
    n = rng.standard_normal(t.size)
    n = np.convolve(n, np.ones(40) / 40, mode="same")  # 粗糙低通
    return n * np.exp(-t * 4)


def write_f32(x: np.ndarray, path: Path) -> Path:
    x.astype(np.float32).tofile(path)
    return path


def mux(out: Path, video_src: str, vf: str, duration: float, audios: list[tuple[np.ndarray, str]], tmp: Path) -> None:
    """lavfi 画面 + 若干条 float32 音轨 → mp4（每条音轨带 title 元数据，模拟 OBS 多轨）。"""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", video_src]  # 时长由输出端 -t 截断
    for i, (a, _) in enumerate(audios):
        cmd += ["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(write_f32(a, tmp / f"a{i}.f32"))]
    cmd += ["-map", "0:v"]
    for i in range(len(audios)):
        cmd += ["-map", f"{i + 1}:a"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
    for i, (_, title) in enumerate(audios):
        cmd += [f"-metadata:s:a:{i}", f"title={title}"]
    cmd += ["-t", f"{duration:.4f}", "-movflags", "+faststart", str(out)]
    run(cmd)


def timecode_vf(label: str) -> str:
    return (
        f"drawtext=fontfile={MONO_FONT}:fontsize=32:fontcolor=white:box=1:boxcolor=black@0.6:x=20:y=20:text='%{{pts\\:hms}}',"
        f"drawtext=fontfile={MONO_FONT}:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6:x=20:y=h-46:text='{label}'"
    )


def flash_vf(times: list[tuple[float, float]]) -> str:
    return ",".join(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.7:t=fill:enable='between(t,{a:.3f},{b:.3f})'" for a, b in times)


def build_game_commentary(out: Path, seed: int) -> dict:
    """游戏实况：人声与游戏声分轨（a:0 mix / a:1 mic / a:2 game）。
    人声长停顿分两类：boring（没人说话也没发生事）应剪，action（没说话但游戏里在打）应保留。"""
    rng = np.random.default_rng(seed)
    voice_items = [
        ("gap", 1.0, None), ("say", "好，我们继续上一期的进度，现在是第三关。", None), ("gap", 0.5, None),
        ("say", "这里要先把左边的箱子推开。", None), ("gap", 6.0, "boring"),
        ("say", "好，到了。", None), ("gap", 0.4, None), ("say", "注意，这个怪会突然冲过来。", None),
        ("gap", 5.0, "action"), ("say", "呼，好险！", None), ("gap", 0.4, None),
        ("say", "刚才那一下差点就没了。", None), ("gap", 7.0, "boring"),
        ("say", "接下来去找钥匙。", None), ("gap", 1.5, None),
    ]
    segs, parts, t = [], [], 0.0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for kind, payload, tag in voice_items:
            clip = synth(payload, "Tingting", tmp) if kind == "say" else np.zeros(int(round(payload * SR)), np.float32)
            d = clip.size / SR
            seg = {"kind": "speech" if kind == "say" else "gap", "start": round(t, 4), "end": round(t + d, 4)}
            if kind == "say":
                seg["text"] = payload
            if tag:
                seg["tag"] = tag
            segs.append(seg)
            parts.append(clip)
            t += d
        mic = np.concatenate(parts) * 10 ** (-4 / 20)
        dur = mic.size / SR
        game = chiptune(dur, -24.0).astype(np.float64)
        events = []
        for s in segs:
            if s.get("tag") == "action":
                for k in range(3):  # 打斗：三连爆炸
                    et = s["start"] + 1.0 + k * 1.1
                    _place(game, explosion(rng), et, 0.9)
                    events.append({"kind": "explosion", "start": round(et, 3), "end": round(et + 1.0, 3)})
        game = game.astype(np.float32)
        mix = np.clip(mic + game, -1, 1).astype(np.float32)
        mp4 = out / "game_commentary.mp4"
        vf = timecode_vf("autocut fixture game_commentary") + "," + flash_vf([(e["start"], e["start"] + 0.25) for e in events])
        mux(mp4, "life=s=640x360:r=30:mold=10:ratio=0.12:life_color=#33ff66:death_color=#202040,scale=640:360", vf, dur,
            [(mix, "mix"), (mic.astype(np.float32), "mic"), (game, "game")], tmp)
    truth = {
        "name": "game_commentary", "file": mp4.name, "duration": round(dur, 4), "fps": 30, "sample_rate": SR,
        "audio_tracks": [{"index": 0, "title": "mix"}, {"index": 1, "title": "mic"}, {"index": 2, "title": "game"}],
        "segments": segs, "events": events,
        "ideal": "保留全部 speech 与 action 间隔（游戏里在打）；boring 间隔（≥5s 无解说无事件）剪掉或压缩到 ≤1s。",
    }
    (out / "game_commentary.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n")
    log.info("game_commentary: %.2fs, events=%d", dur, len(events))
    return truth


def build_stream_vod(out: Path, seed: int) -> dict:
    """直播录像（~150s）+ 弹幕日志。3 个真高光（大喊+笑），其中第 3 个没有弹幕高峰；
    另有一次抽奖刷屏（弹幕高峰但画面音频平平），是诱饵。"""
    rng = np.random.default_rng(seed)
    dur = 150.0
    chatter = [
        (3.0, "欢迎新来的朋友，今天我们继续冲分。"), (14.0, "这把感觉对面有点强。"), (22.0, "我先买个装备。"),
        (48.0, "弹幕说要我开麦，已经开了啊。"), (58.0, "好，现在开个抽奖，扣一参与。"), (72.0, "这个位置一般没人来。"),
        (100.0, "大家吃饭了吗？"), (110.0, "等一下，我看看地图。"), (120.0, "这把稳一点打。"), (143.0, "好，下一把。"),
    ]
    highlights_spec = [
        (33.0, "我去！这也能秒？！", True), (83.0, "不是吧！一打三！一打三啊！", True), (131.0, "完了完了完了，诶，赢了？！", False),
    ]
    voice = np.zeros(int(round(dur * SR)), dtype=np.float64)
    speech, highlights = [], []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for t0, text in chatter:
            c = synth(text, "Tingting", tmp)
            _place(voice, c, t0, 10 ** (-10 / 20))
            speech.append({"start": t0, "end": round(t0 + c.size / SR, 3), "text": text})
        for t0, text, _ in highlights_spec:
            c = synth(text, "Tingting", tmp)
            _place(voice, c, t0, 1.0)
            laugh = synth("哈哈哈哈哈哈哈哈", "Tingting", tmp)
            t_l = t0 + c.size / SR + 0.15
            _place(voice, laugh, t_l, 0.9)
            end = t_l + laugh.size / SR
            highlights.append({"start": round(t0, 3), "end": round(end, 3), "text": text})
        game = chiptune(dur, -30.0).astype(np.float64)
        for h in highlights:
            for k in range(2):
                _place(game, explosion(rng), h["start"] - 1.5 + k * 0.6, 0.8)
        mix = np.clip(voice + game, -1, 1).astype(np.float32)
        mp4 = out / "stream_vod.mp4"
        vf = timecode_vf("LIVE  autocut fixture stream_vod") + "," + flash_vf([(h["start"], h["start"] + 0.4) for h in highlights])
        mux(mp4, "testsrc2=s=640x360:r=30", vf, dur, [(mix, "mix")], tmp)
    # 弹幕：泊松到达；高光 1、2 后 12s 内密度飙升；58–70s 抽奖刷屏是诱饵
    normal = ["666", "哈哈", "主播加油", "这把能赢吗", "？", "来了来了", "好家伙", "前排"]
    hype = ["卧槽", "666666", "名场面", "切片！", "哈哈哈哈哈", "这也行？", "牛"]
    chat = []
    t = 0.0
    while t < dur:
        t += rng.exponential(1 / 0.4)
        chat.append({"t": round(t, 2), "user": f"u{rng.integers(1000, 9999)}", "text": str(rng.choice(normal))})
    for (h, spec) in zip(highlights, highlights_spec):
        if spec[2]:
            t = h["start"] + 1.0
            while t < h["start"] + 13:
                t += rng.exponential(1 / 4.0)
                chat.append({"t": round(t, 2), "user": f"u{rng.integers(1000, 9999)}", "text": str(rng.choice(hype))})
    t = 59.5
    while t < 70:
        t += rng.exponential(1 / 5.0)
        chat.append({"t": round(t, 2), "user": f"u{rng.integers(1000, 9999)}", "text": "1"})
    chat = sorted([c for c in chat if c["t"] < dur], key=lambda c: c["t"])
    (out / "stream_vod.chat.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chat))
    truth = {
        "name": "stream_vod", "file": mp4.name, "duration": dur, "fps": 30, "sample_rate": SR, "chat_file": "stream_vod.chat.jsonl",
        "highlights": highlights, "chatter": speech, "decoys": [{"kind": "chat_spam_lottery", "start": 59.5, "end": 70.0}],
        "ideal": "3 个切片各覆盖一个 highlight（含前 3–10s 铺垫），时长 15–30s；不选抽奖刷屏段；竖屏 9:16 时画面主体不丢。",
    }
    (out / "stream_vod.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n")
    log.info("stream_vod: %.0fs, highlights=%s, chat=%d", dur, [h["start"] for h in highlights], len(chat))
    return truth


def build_montage(out: Path, seed: int) -> dict:
    """混剪素材：6 段视觉差异明显的 4s 片段（自带环境底噪）+ 120 BPM 鼓点 BGM（节拍真值已知）。"""
    d = out / "montage"
    d.mkdir(exist_ok=True)
    sources = [
        "mandelbrot=s=640x360:r=30", "life=s=640x360:r=30:mold=10:ratio=0.1,scale=640:360",
        "cellauto=s=640x360:r=30:rule=110,scale=640:360", "gradients=s=640x360:r=30:speed=0.08",
        "testsrc2=s=640x360:r=30", "smptehdbars=s=640x360:r=30,hue=H=2*PI*t/4",
    ]
    clips = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i, src in enumerate(sources, 1):
            p = d / f"clip{i}.mp4"
            amb = pink_noise(int(4.0 * SR), -40.0, seed + i)
            vf = f"drawtext=fontfile={MONO_FONT}:fontsize=72:fontcolor=white:box=1:boxcolor=black@0.5:x=(w-tw)/2:y=(h-th)/2:text='CLIP {i}'"
            mux(p, src, vf, 4.0, [(amb, "ambient")], tmp)
            clips.append(p.name)
    music, beats = drum_loop(120, 12, seed)
    music_path = d / "music_120bpm.wav"
    run_wav(music, music_path)
    truth = {
        "name": "montage", "dir": "montage", "clips": clips, "music": music_path.name, "bpm": 120,
        "music_duration": round(music.size / SR, 4), "beats": beats, "bar_starts": beats[::4],
        "ideal": "约 15s；画面切点落在节拍上（±1 帧）；用到 ≥4 个不同片段；音频是 BGM 而不是片段自带底噪。",
    }
    (out / "montage.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n")
    log.info("montage: %d clips, music %.1fs, %d beats", len(clips), music.size / SR, len(beats))
    return truth


def run_wav(x: np.ndarray, path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = write_f32(x, Path(td) / "x.f32")
        run(["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(raw), "-c:a", "pcm_s16le", str(path)])


KNOWLEDGE_SCRIPT = [
    "为什么天空是蓝色的？",
    "太阳光看起来是白色，其实混合了各种颜色的光。",
    "阳光穿过大气层时，会撞上空气里的氮气和氧气分子。",
    "波长越短的光，被这些分子散射得越厉害，这叫瑞利散射。",
    "蓝光的波长比红光短得多，所以被散射到了整个天空。",
    "到了傍晚，阳光要穿过更厚的大气，蓝光在路上被散射光了，剩下的红光和橙光，就染红了晚霞。",
]


def build_knowledge(out: Path, seed: int) -> dict:
    """知识视频素材：文稿 + 3 张配图。真值给出逐句文本和用 say 朗读的参考时长。"""
    d = out / "knowledge"
    d.mkdir(exist_ok=True)
    (d / "script.md").write_text("# 为什么天空是蓝色的\n\n" + "\n".join(KNOWLEDGE_SCRIPT) + "\n")
    titles = [("太阳光 = 七色光", "0x1d3557"), ("瑞利散射", "0x2a6f97"), ("日落为什么是红的", "0xb5523b")]
    for i, (title, color) in enumerate(titles, 1):
        tf = d / f".title{i}.txt"
        tf.write_text(title)
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=1280x720",
             "-vf", f"drawtext=fontfile='{CJK_FONT}':textfile='{tf}':fontsize=84:fontcolor=white:x=(w-tw)/2:y=(h-th)/2",
             "-frames:v", "1", str(d / f"img{i}.png")])
        tf.unlink()
    with tempfile.TemporaryDirectory() as td:
        ref = sum(synth(s, "Tingting", Path(td)).size / SR for s in KNOWLEDGE_SCRIPT)
    truth = {
        "name": "knowledge", "dir": "knowledge", "script": "script.md", "images": ["img1.png", "img2.png", "img3.png"],
        "sentences": KNOWLEDGE_SCRIPT, "reference_narration_seconds": round(ref, 2),
        "ideal": "横屏视频；全文配音；逐句字幕与配音同步；3 张图都出现且与内容对应；时长≈配音时长。",
    }
    (out / "knowledge.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n")
    log.info("knowledge: %d sentences, ref narration %.1fs", len(KNOWLEDGE_SCRIPT), ref)
    return truth


def build_ytpmv(out: Path, seed: int) -> dict:
    """鬼畜素材：逐字合成的两句台词（音节边界精确已知，画面同步显示当前字）+ 120 BPM BGM。"""
    d = out / "ytpmv"
    d.mkdir(exist_ok=True)
    phrases = [list("我真的会谢"), list("这也太离谱了吧")]
    parts, sylls, t = [np.zeros(int(0.8 * SR), np.float32)], [], 0.8
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for pi, ph in enumerate(phrases):
            for ch in ph:
                c = synth(ch, "Tingting", tmp)
                sylls.append({"text": ch, "phrase": pi, "start": round(t, 4), "end": round(t + c.size / SR, 4)})
                parts += [c, np.zeros(int(0.06 * SR), np.float32)]
                t += c.size / SR + 0.06
            parts.append(np.zeros(int(1.0 * SR), np.float32))
            t += 1.0
        voice = np.concatenate(parts)
        voice = voice + pink_noise(voice.size, -55.0, seed)
        dur = voice.size / SR
        vf = timecode_vf("autocut fixture ytpmv source")
        for s in sylls:
            vf += (f",drawtext=fontfile='{CJK_FONT}':text='{s['text']}':fontsize=160:fontcolor=yellow:"
                   f"x=(w-tw)/2:y=(h-th)/2:enable='between(t,{s['start']:.3f},{s['end']:.3f})'")
        mux(d / "source_clip.mp4", "testsrc2=s=640x360:r=30", vf, dur, [(voice.astype(np.float32), "voice")], tmp)
    music, beats = drum_loop(120, 8, seed + 7)
    run_wav(music, d / "music_120bpm.wav")
    truth = {
        "name": "ytpmv", "dir": "ytpmv", "source": "source_clip.mp4", "music": "music_120bpm.wav", "bpm": 120,
        "syllables": sylls, "beats": beats, "music_duration": round(music.size / SR, 4),
        "ideal": "输出以 BGM 为底；台词音节被切开并在节拍（或半拍）上重复出现 ≥6 次；画面与对应音节同步切换。",
    }
    (out / "ytpmv.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n")
    log.info("ytpmv: %d syllables, source %.1fs", len(sylls), dur)
    return truth


BUILDERS = {
    "game_commentary": build_game_commentary,
    "stream_vod": build_stream_vod,
    "montage": build_montage,
    "knowledge": build_knowledge,
    "ytpmv": build_ytpmv,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "media")
    ap.add_argument("--only", nargs="*", help="只生成这些素材")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    a.out.mkdir(parents=True, exist_ok=True)
    for i, (name, spec) in enumerate(SCRIPTS.items()):
        if a.only and name not in a.only:
            continue
        build(name, spec, a.out, seed=1000 + i)
    for i, (name, fn) in enumerate(BUILDERS.items()):
        if a.only and name not in a.only:
            continue
        fn(a.out, seed=2000 + i)


if __name__ == "__main__":
    main()
