"""媒体探测：ffprobe 的薄封装，把输出整理成不可变的 dataclass。"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# 抽样检测 VFR 时读的视频包数：够覆盖几秒内的帧间隔分布，又不会让 probe 变慢
_VFR_SAMPLE_PACKETS = 300
# 帧间隔偏离中位数超过这个比例才算"不均匀"（容器时间基取整只会造成 <5% 的抖动）
_VFR_JITTER_REL = 0.15
# 不均匀的帧间隔占比超过这个值才判 VFR（容忍偶发丢帧）
_VFR_JITTER_SHARE = 0.02


class MediaError(RuntimeError):
    """媒体文件无法被 ffprobe / ffmpeg 处理。消息里带文件名、关键报错行和可能的原因。"""


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    is_vfr: bool
    codec: str
    rotation: int  # 显示时需顺时针旋转的角度：0 / 90 / 180 / 270（iPhone 竖拍视频为 90）
    display_aspect: float  # 考虑 SAR 与旋转之后的显示宽高比


@dataclass(frozen=True)
class AudioTrack:
    stream_index: int  # 容器内的流序号（ffmpeg -map 0:<stream_index>）
    track_number: int  # 按音轨顺序的序号，0 起（ffmpeg -map 0:a:<track_number>）
    title: str | None
    channels: int
    sample_rate: int
    codec: str
    duration: float


@dataclass(frozen=True)
class MediaInfo:
    path: str
    duration: float
    format_name: str
    video: VideoInfo | None
    audio: tuple[AudioTrack, ...]


# ------------------------------------------------------------------ 解析


def _ratio(s: str | None) -> float | None:
    """"30000/1001" / "16:9" → float；0/0、N/A、缺失 → None。"""
    if not s:
        return None
    for sep in ("/", ":"):
        if sep in s:
            a, _, b = s.partition(sep)
            try:
                num, den = float(a), float(b)
            except ValueError:
                return None
            return num / den if den else None
    try:
        return float(s)
    except ValueError:
        return None


def _rotation(stream: dict) -> int:
    """显示矩阵 / rotate tag → 顺时针角度。ffprobe 的 rotation 是逆时针（iPhone 竖拍报 -90）。"""
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            return int(round(-float(sd["rotation"]))) % 360 // 90 * 90
    tag = (stream.get("tags") or {}).get("rotate")
    if tag is not None:
        try:
            return int(round(float(tag))) % 360 // 90 * 90
        except ValueError:
            pass
    return 0


def _display_aspect(stream: dict, rotation: int) -> float:
    w, h = int(stream.get("width") or 0), int(stream.get("height") or 0)
    dar = _ratio(stream.get("display_aspect_ratio"))
    if dar is None and w and h:
        sar = _ratio(stream.get("sample_aspect_ratio")) or 1.0
        dar = w * sar / h
    dar = dar or 0.0
    return 1.0 / dar if rotation in (90, 270) and dar else dar


def _is_vfr(r_rate: float | None, avg_rate: float | None, pts: np.ndarray | None) -> bool:
    """优先用抽样 pts 的帧间隔判断；样本不足时退回比较 r/avg_frame_rate。"""
    if pts is not None and len(pts) >= 8:
        d = np.diff(np.sort(pts))
        d = d[d > 0]
        if len(d) >= 7:
            med = float(np.median(d))
            return float(np.mean(np.abs(d - med) > _VFR_JITTER_REL * med)) > _VFR_JITTER_SHARE
    if r_rate and avg_rate:
        return abs(r_rate - avg_rate) / r_rate > 0.1
    return False


def parse_probe(data: dict, path: str | Path, video_pts: np.ndarray | None = None) -> MediaInfo:
    """把 ffprobe -show_format -show_streams 的 JSON 整理成 MediaInfo（纯函数，便于单测）。"""
    fmt = data.get("format")
    if not fmt:
        raise MediaError(f"无法读取 {path}：ffprobe 没有返回 format 信息（文件可能为空、被截断或不是媒体文件）")
    duration = float(fmt.get("duration") or 0.0)

    video: VideoInfo | None = None
    audio: list[AudioTrack] = []
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        if kind == "video" and video is None and (s.get("disposition") or {}).get("attached_pic", 0) != 1:
            r_rate, avg_rate = _ratio(s.get("r_frame_rate")), _ratio(s.get("avg_frame_rate"))
            rot = _rotation(s)
            vfr = _is_vfr(r_rate, avg_rate, video_pts)
            # CFR 用 r_frame_rate（精确）；VFR 用 avg_frame_rate（代表平均帧率）。短片的 avg 会被首尾帧时长拉偏
            fps = (avg_rate or r_rate) if vfr else (r_rate or avg_rate)
            video = VideoInfo(
                width=int(s.get("width") or 0),
                height=int(s.get("height") or 0),
                fps=float(fps or 0.0),
                is_vfr=vfr,
                codec=str(s.get("codec_name") or ""),
                rotation=rot,
                display_aspect=_display_aspect(s, rot),
            )
        elif kind == "audio":
            tags = s.get("tags") or {}
            # mp4/mov 把音轨标题存在 name 原子里，ffprobe 报成 tags.name
            title = tags.get("title") or tags.get("name")
            audio.append(
                AudioTrack(
                    stream_index=int(s["index"]),
                    track_number=len(audio),
                    title=title,
                    channels=int(s.get("channels") or 0),
                    sample_rate=int(s.get("sample_rate") or 0),
                    codec=str(s.get("codec_name") or ""),
                    duration=float(s.get("duration") or duration),
                )
            )
    return MediaInfo(
        path=str(path),
        duration=duration,
        format_name=str(fmt.get("format_name") or ""),
        video=video,
        audio=tuple(audio),
    )


# ------------------------------------------------------------------ ffprobe 调用


def _run_ffprobe(path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["ffprobe", "-v", "error", *args, str(path)]
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise MediaError("找不到 ffprobe：请安装 ffmpeg（macOS: brew install ffmpeg）并确认它在 PATH 里") from e


def key_error_line(stderr: str) -> str:
    """从 ffmpeg/ffprobe 的 stderr 里挑最能说明问题的一行（最后一条非空行）。"""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else "（ffprobe 没有输出报错信息）"


def probe_failure(path: Path, stderr: str, tool: str = "ffprobe") -> MediaError:
    """统一的"处理失败"异常：文件名 + 关键报错行 + 可能原因。"""
    if not path.exists():
        return MediaError(f"文件不存在：{path}（{tool} 未运行）。请检查路径拼写与当前工作目录")
    return MediaError(
        f"{tool} 无法处理 {path}：{key_error_line(stderr)}。"
        "可能原因：文件损坏或下载/录制未完成（如 mp4 缺 moov atom）、不是音视频文件、无读取权限"
    )


def _sample_video_pts(path: Path) -> np.ndarray | None:
    r = _run_ffprobe(
        path,
        ["-select_streams", "v:0", "-show_entries", "packet=pts_time", "-read_intervals", f"%+#{_VFR_SAMPLE_PACKETS}",
         "-of", "csv=p=0"],
    )
    if r.returncode != 0:
        return None
    vals = []
    for ln in r.stdout.split():
        try:
            vals.append(float(ln.rstrip(",")))
        except ValueError:
            continue
    return np.asarray(vals) if vals else None


def probe(path: str | Path) -> MediaInfo:
    """探测媒体文件。失败（不存在 / 损坏 / 非媒体）抛 MediaError。"""
    path = Path(path)
    t0 = time.perf_counter()
    r = _run_ffprobe(path, ["-show_format", "-show_streams", "-of", "json"])
    if r.returncode != 0:
        raise probe_failure(path, r.stderr)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise MediaError(f"ffprobe 对 {path} 的输出不是合法 JSON：{e}") from e
    has_video = any(s.get("codec_type") == "video" for s in data.get("streams") or [])
    info = parse_probe(data, path, _sample_video_pts(path) if has_video else None)
    log.debug(
        "probe %s: %.2fs format=%s video=%s audio=%d 条 (%.0fms)",
        path.name, info.duration, info.format_name, info.video, len(info.audio), (time.perf_counter() - t0) * 1e3,
    )
    return info
