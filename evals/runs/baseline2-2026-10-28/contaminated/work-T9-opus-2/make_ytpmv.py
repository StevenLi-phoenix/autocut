#!/usr/bin/env python3
"""鬼畜生成：把 source_clip 的台词按音节切开，按 16 分音符网格卡在 120 BPM 的拍子上。

仅依赖 Python 标准库 + ffmpeg。
用法: python3 make_ytpmv.py [--work /tmp/ytpmv_work]
"""
import argparse, array, math, os, subprocess, sys, wave

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "ytpmv", "source_clip.mp4")
MUSIC = os.path.join(HERE, "ytpmv", "music_120bpm.wav")
OUT = os.path.join(HERE, "ytpmv.mp4")

SR = 48000
FPS = 30
BEAT = 0.5          # 120 BPM，已核对：音乐每 0.5s 一个硬起音，从 t=0 开始
TOTAL_BEATS = 32    # 16s = 8 小节

# 音节边界：按能量包络手工核对（音节间有 >40ms 的静音），并用画面上显示的字逐个验证。
# 缓存里 whisper 的词时间戳错位一个音节，且把「谢」识别成了「写」，所以不用它。
SYL = {
    "我": (0.79, 1.08), "真": (1.15, 1.47), "的": (1.51, 1.67), "会": (1.75, 2.02),
    "谢": (2.07, 2.37), "这": (3.43, 3.67), "也": (3.71, 3.99), "太": (4.03, 4.33),
    "离": (4.385, 4.73), "谱": (4.77, 5.08), "了": (5.13, 5.33), "吧": (5.37, 5.60),
}


def roll(start, syl, n, step=0.25):
    """16 分音符连打，音高逐个升 2 个半音。"""
    return [(start + i * step, syl, 2 * i) for i in range(n)]


def seq(start, syls, step):
    return [(start + i * step, s, 0) for i, s in enumerate(syls)]


# (拍位置, 音节, 升调半音)
SCORE = (
    # 第 1 小节：我 · 我 · 我我 我我我我（加速）
    seq(0, "我我", 1) + seq(2, "我我", 0.5) + roll(3, "我", 4)
    # 第 2 小节：真的会谢 谢 谢 谢谢谢谢
    + seq(4, "真的会谢谢谢", 0.5) + roll(7, "谢", 4)
    # 第 3 小节：正常速度念一遍
    + [(8, "我", 0), (9, "真", 0), (9.5, "的", 0), (10, "会", 0), (11, "谢", 0)]
    # 第 4 小节：8 分音符快念 + 「的的的的」过门
    + seq(12, "我真的会谢谢", 0.5) + roll(15, "的", 4)
    # 第 5 小节：这也太 离谱了吧
    + seq(16, "这也太", 0.5) + seq(18, "离谱了吧", 0.5)
    # 第 6 小节：太太太太 离离离离 谱
    + seq(20, "太太太太", 0.5) + roll(22, "离", 4) + [(23, "谱", 0)]
    # 第 7 小节：这也太离谱了吧 吧
    + seq(24, "这也太离谱了吧吧", 0.5)
    # 第 8 小节：离 离 离离离离 谱 会谢（呼应开头）
    + seq(28, "离离", 0.5) + roll(29, "离", 4) + [(30, "谱", 0), (31, "会", 0), (31.5, "谢", 0)]
)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd):
    subprocess.run(cmd, check=True)


def read_wav(path):
    w = wave.open(path)
    assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, path
    return array.array("h", w.readframes(w.getnframes()))


def to_wav(src, dst, extra_af=None):
    af = ["-af", extra_af] if extra_af else []
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn",
         *af, "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", dst])


def build_events():
    ev = sorted(SCORE)
    out = []
    for i, (b, s, semi) in enumerate(ev):
        nb = ev[i + 1][0] if i + 1 < len(ev) else TOTAL_BEATS
        assert nb > b, f"重叠事件 @beat {b}"
        out.append(dict(t0=b * BEAT, t1=nb * BEAT, syl=s, semi=semi, beat=b))
    # 同一音节连续重复时交替镜像
    run_i = 0
    for i, e in enumerate(out):
        run_i = run_i + 1 if i and out[i - 1]["syl"] == e["syl"] else 0
        e["flip"] = run_i % 2 == 1
        e["punch"] = e["beat"] % 4 == 0  # 每小节强拍缩放冲击
    return out


def render_audio(events, work):
    src = read_wav(os.path.join(work, "src.wav"))
    music = read_wav(os.path.join(work, "music.wav"))
    n = int(TOTAL_BEATS * BEAT * SR)
    voice = [0.0] * n
    for k, e in enumerate(events):
        a, b = SYL[e["syl"]]
        clip = [x / 32768 for x in src[int(a * SR):int(b * SR)]]
        if e["semi"]:
            # asetrate 变调：音高升高同时变短，经典鬼畜音色
            raw = os.path.join(work, f"p_{k:03d}.raw")
            r = 2 ** (e["semi"] / 12)
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1",
                 "-i", "pipe:0", "-af", f"asetrate={SR * r:.0f},aresample={SR}", "-f", "s16le", raw, "-y"],
                input=array.array("h", src[int(a * SR):int(b * SR)]).tobytes(), check=True)
            clip = [x / 32768 for x in array.array("h", open(raw, "rb").read())]
        slot = int((e["t1"] - e["t0"]) * SR)
        truncated = len(clip) > slot
        clip = clip[:slot]
        fi, fo = int(0.002 * SR), int((0.012 if truncated else 0.005) * SR)
        L = len(clip)
        for i in range(min(fi, L)):
            clip[i] *= i / fi
        for i in range(min(fo, L)):
            clip[L - 1 - i] *= i / fo
        start = int(e["t0"] * SR)
        for i, x in enumerate(clip):
            if start + i < n:
                voice[start + i] += x
        e["audio_len"] = L / SR
        e["truncated"] = truncated
    VOICE_GAIN, MUSIC_GAIN = 1.0, 0.55
    mix = [VOICE_GAIN * voice[i] + MUSIC_GAIN * (music[i] / 32768 if i < len(music) else 0) for i in range(n)]
    peak = max(abs(x) for x in mix)
    g = 10 ** (-1 / 20) / peak if peak > 10 ** (-1 / 20) else 1.0
    log(f"[audio] mix peak {20 * math.log10(peak):.2f} dBFS -> gain {20 * math.log10(g):+.2f} dB")
    out = array.array("h", (max(-32767, min(32767, int(x * g * 32767))) for x in mix))
    path = os.path.join(work, "mix.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(out.tobytes())
    return path


def render_video(events, work):
    lst = []
    for k, e in enumerate(events):
        a, b = SYL[e["syl"]]
        nframes = round(e["t1"] * FPS) - round(e["t0"] * FPS)  # 累计取整，避免 7.5/3.75 帧的漂移
        syl_frames = max(1, round((b - a) * FPS))
        take = min(nframes, syl_frames)
        vf = [f"trim=start={a}:end={a + take / FPS + 0.001}", "setpts=PTS-STARTPTS", f"fps={FPS}"]
        if nframes > take:
            vf.append(f"tpad=stop_mode=clone:stop={nframes - take}")
        if e["flip"]:
            vf.append("hflip")
        if e["punch"]:
            vf += ["scale=w='trunc(640*(1+0.25*exp(-10*t))/2)*2':h='trunc(360*(1+0.25*exp(-10*t))/2)*2':eval=frame",
                   "crop=640:360"]
        vf.append("format=yuv420p")
        seg = os.path.join(work, f"v_{k:03d}.mp4")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", SRC, "-an", "-vf", ",".join(vf),
             "-frames:v", str(nframes), "-r", str(FPS), "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", seg])
        e["frames"] = nframes
        lst.append(f"file '{seg}'")
    with open(os.path.join(work, "concat.txt"), "w") as f:
        f.write("\n".join(lst) + "\n")
    return os.path.join(work, "concat.txt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/tmp/ytpmv_work")
    args = ap.parse_args()
    os.makedirs(args.work, exist_ok=True)
    to_wav(SRC, os.path.join(args.work, "src.wav"))
    to_wav(MUSIC, os.path.join(args.work, "music.wav"))

    events = build_events()
    log(f"[score] {len(events)} events, {TOTAL_BEATS} beats @ {60 / BEAT:.0f} BPM")
    mix = render_audio(events, args.work)
    concat = render_video(events, args.work)
    for e in events:
        log(f"  beat {e['beat']:5.2f}  t={e['t0']:6.3f}-{e['t1']:6.3f}  {e['syl']}"
            f"{' +%dst' % e['semi'] if e['semi'] else '':6s} frames={e['frames']:2d}"
            f" audio={e['audio_len']:.3f}{' (cut)' if e['truncated'] else ''}"
            f"{' flip' if e['flip'] else ''}{' punch' if e['punch'] else ''}")
    total_frames = sum(e["frames"] for e in events)
    assert total_frames == TOTAL_BEATS * BEAT * FPS, total_frames
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", concat,
         "-i", mix, "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", OUT])
    log(f"[done] {OUT} ({total_frames} frames)")


if __name__ == "__main__":
    main()
