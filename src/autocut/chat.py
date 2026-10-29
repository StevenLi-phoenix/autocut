"""弹幕/聊天日志：加载、密度曲线、刷屏度。

弹幕密度高 ≠ 高光：抽奖时全场刷同一个"1"，密度比真正的名场面还高。
密度回答"有多热闹"，burstiness 回答"是不是在复读同一句"，两者要一起看。
"""

import json
import logging
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatMessage:
    t: float  # 相对视频开头的秒数
    user: str
    text: str


def load_chat(path: str | Path) -> list[ChatMessage]:
    """读 JSONL 弹幕（每行 {"t","user","text"}，user 可省），按时间升序返回。

    空行跳过；某行格式不对抛 ValueError，消息里带 文件:行号。文件不存在抛 FileNotFoundError。
    """
    path = Path(path)
    msgs: list[ChatMessage] = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                t = float(d["t"])
                text = str(d["text"])
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: 不是合法的 JSON（{e.msg}）：{line[:60]!r}") from e
            except KeyError as e:
                raise ValueError(f"{path}:{lineno}: 缺少字段 {e.args[0]}（需要 t 和 text）：{line[:60]!r}") from e
            except (TypeError, ValueError) as e:
                raise ValueError(f"{path}:{lineno}: 字段类型不对（t 要是数字）：{line[:60]!r}") from e
            if not math.isfinite(t):
                raise ValueError(f"{path}:{lineno}: t 不是有限数：{t}")
            msgs.append(ChatMessage(t=t, user=str(d.get("user", "")), text=text))
    msgs.sort(key=lambda m: m.t)
    log.debug("load_chat %s: %d 条, 时间范围 %s", path.name, len(msgs), (msgs[0].t, msgs[-1].t) if msgs else None)
    return msgs


def chat_density(
    messages: Sequence[ChatMessage], duration: float, hop: float = 1.0, window: float = 5.0
) -> tuple[np.ndarray, np.ndarray]:
    """弹幕密度曲线 (times, rate)：times = 0, hop, 2hop, …≤duration；
    rate[i] = 以 times[i] 为中心、宽 window 的窗口 [t-w/2, t+w/2) 内的消息数 / window（条/秒）。"""
    times = np.arange(int(math.floor(duration / hop + 1e-9)) + 1) * hop
    ts = np.sort(np.fromiter((m.t for m in messages), dtype=np.float64, count=len(messages)))
    counts = np.searchsorted(ts, times + window / 2, side="left") - np.searchsorted(ts, times - window / 2, side="left")
    return times, counts / window


def messages_between(messages: Sequence[ChatMessage], start: float, end: float) -> list[ChatMessage]:
    """时间落在 [start, end) 的消息。"""
    return [m for m in messages if start <= m.t < end]


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def distinct_ratio(messages: Sequence[ChatMessage]) -> float:
    """不同文本数 / 消息数 ∈ (0, 1]。越低越像复读。没有消息时返回 1.0。
    注意它随窗口变长而自然变小（词汇有限），跨不同长度的窗口比较时用 burstiness。"""
    if not messages:
        return 1.0
    return len({_norm(m.text) for m in messages}) / len(messages)


def burstiness(messages: Sequence[ChatMessage]) -> float:
    """刷屏度 ∈ [0, 1]：出现最多的那句话占全部消息的比例。1 = 全在刷同一句（抽奖刷"1"），
    接近 0 = 各说各的（多样化的惊叹）。少于 2 条消息不构成刷屏，返回 0。"""
    if len(messages) < 2:
        return 0.0
    top = Counter(_norm(m.text) for m in messages).most_common(1)[0][1]
    return top / len(messages)
