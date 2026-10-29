"""verify / verify_genres 自测共用的 ffmpeg 助手（不是测试模块，不会被 pytest 收集）。"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"


def ff(*args: str) -> None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def merge(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in ranges:
        if b - a < 1e-3:
            continue
        if out and abs(out[-1][1] - a) < 1e-6:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def render_ranges(src: Path, dst: Path, ranges: list[tuple[float, float]], audio_stream: int = 0) -> Path:
    """按源区间拼接（视频 + 指定音轨），输出单音轨 mp4。"""
    parts, labels = [], ""
    for i, (a, b) in enumerate(ranges):
        parts.append(f"[0:v]trim={a:.4f}:{b:.4f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a:{audio_stream}]atrim={a:.4f}:{b:.4f},asetpts=PTS-STARTPTS[a{i}]")
        labels += f"[v{i}][a{i}]"
    fc = ";".join(parts) + f";{labels}concat=n={len(ranges)}:v=1:a=1[v][a]"
    ff("-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a", "aac", str(dst))
    return dst


def vertical_clip(src: Path, dst: Path, start: float, dur: float, vertical: bool = True) -> Path:
    vf = "scale=540:960:force_original_aspect_ratio=increase,crop=540:960" if vertical else "scale=640:360"
    ff("-i", str(src), "-ss", f"{start}", "-t", f"{dur}", "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a", "aac", str(dst))
    return dst


def write_srt(dst: Path, lines: list[tuple[float, float, str]]) -> Path:
    def ts(t: float) -> str:
        return f"{int(t // 3600):02d}:{int(t // 60) % 60:02d}:{int(t) % 60:02d},{int(round((t % 1) * 1000)):03d}"
    dst.write_text("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(lines, 1)))
    return dst


def montage(clips_dir: Path, dst: Path, plan: list[tuple[int, float]], bgm: Path | None, bgm_offset: float = 0.0) -> Path:
    """plan = [(clip 序号, 时长)]；音频用 bgm 的 [offset, offset+总长]，bgm=None 时用片段自带音轨。"""
    inputs, v, a = [], [], []
    for i, (n, d) in enumerate(plan):
        inputs += ["-t", f"{d}", "-i", str(clips_dir / f"clip{n}.mp4")]
        v.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
        a.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
    cat_v = "".join(f"[v{i}]" for i in range(len(plan))) + f"concat=n={len(plan)}:v=1:a=0[v]"
    total = sum(d for _, d in plan)
    if bgm is not None:
        ff(*inputs, "-ss", f"{bgm_offset}", "-t", f"{total}", "-i", str(bgm), "-filter_complex", ";".join(v + [cat_v]),
           "-map", "[v]", "-map", f"{len(plan)}:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(dst))
    else:
        cat_a = "".join(f"[a{i}]" for i in range(len(plan))) + f"concat=n={len(plan)}:v=0:a=1[a]"
        ff(*inputs, "-filter_complex", ";".join(v + a + [cat_v, cat_a]), "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(dst))
    return dst


def pcm(path: Path, sr: int = 48000) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    assert r.returncode == 0
    return np.frombuffer(r.stdout, dtype="<f4").astype(np.float64)


def mux_audio(samples: np.ndarray, dst: Path, sr: int = 48000, size: str = "320x180") -> Path:
    """float 样本 → wav → 配一条 testsrc2 画面 → mp4。"""
    wav = dst.with_suffix(".wav")
    x = np.clip(samples, -1, 1)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={len(x) / sr:.3f}", "-i", str(wav),
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(dst))
    return dst
