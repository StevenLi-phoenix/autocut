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


if __name__ == "__main__":
    main()
