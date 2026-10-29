"""chat：弹幕加载、密度曲线、刷屏度。"""

import json
import logging

import numpy as np
import pytest

from autocut.chat import ChatMessage, burstiness, chat_density, distinct_ratio, load_chat, messages_between

log = logging.getLogger(__name__)


def _msgs(times: list[float], text: str = "x") -> list[ChatMessage]:
    return [ChatMessage(t=t, user=f"u{i}", text=text) for i, t in enumerate(times)]


# ------------------------------------------------------------------ 单元


@pytest.mark.unit
def test_load_chat_parses_sorts_and_skips_blank_lines(tmp_path) -> None:
    p = tmp_path / "chat.jsonl"
    p.write_text(
        json.dumps({"t": 2.5, "user": "b", "text": "第二条"}, ensure_ascii=False) + "\n"
        "\n"
        + json.dumps({"t": 1.0, "user": "a", "text": "第一条"}, ensure_ascii=False) + "\n"
    )
    msgs = load_chat(p)
    assert msgs == [ChatMessage(1.0, "a", "第一条"), ChatMessage(2.5, "b", "第二条")]  # 乱序输入按时间排好


@pytest.mark.unit
def test_load_chat_reports_file_and_line_on_bad_input(tmp_path) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text('{"t": 1, "user": "a", "text": "ok"}\nnot json\n')
    with pytest.raises(ValueError, match=r"bad\.jsonl.*2"):
        load_chat(p)
    q = tmp_path / "missing_field.jsonl"
    q.write_text('{"t": 1, "user": "a"}\n')
    with pytest.raises(ValueError, match=r"missing_field\.jsonl.*text"):
        load_chat(q)


@pytest.mark.unit
def test_load_chat_missing_file_raises_filenotfound(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_chat(tmp_path / "none.jsonl")


@pytest.mark.unit
def test_chat_density_counts_per_second_in_centered_window() -> None:
    # 10.05s..15.95s 每 0.1s 一条（错开整数边界），窗口 [t-2.5, t+2.5) 内最多 50 条 = 10 条/秒
    msgs = _msgs([10.05 + 0.1 * i for i in range(60)])
    times, rate = chat_density(msgs, duration=30.0, hop=1.0, window=5.0)
    assert len(times) == 31 and times[0] == 0.0 and times[-1] == 30.0
    assert rate[13] == pytest.approx(10.0)
    assert int(np.argmax(rate)) == 13
    assert rate[0] == 0.0 and rate[30] == 0.0
    assert rate.sum() * 1.0 == pytest.approx(len(msgs))  # 每条消息被 window/hop 个窗口各数 1/window


@pytest.mark.unit
def test_chat_density_empty_and_edges() -> None:
    times, rate = chat_density([], duration=10.0)
    assert len(times) == 11 and not rate.any()
    times, rate = chat_density(_msgs([0.0, 10.0]), duration=10.0, hop=1.0, window=2.0)
    assert rate[0] > 0 and rate[-1] > 0  # 首尾的消息不丢


@pytest.mark.unit
def test_distinct_ratio_and_burstiness_separate_spam_from_variety() -> None:
    spam = _msgs([0.1 * i for i in range(60)], text="1")
    variety_texts = ["卧槽", "这也行？", "哈哈哈哈哈", "牛", "名场面", "666666", "切片！", "来了来了"]
    variety = [ChatMessage(0.1 * i, f"u{i}", variety_texts[i % len(variety_texts)]) for i in range(32)]
    assert burstiness(spam) == pytest.approx(1.0) and distinct_ratio(spam) == pytest.approx(1 / 60)
    assert burstiness(variety) == pytest.approx(0.125) and distinct_ratio(variety) == pytest.approx(0.25)
    assert burstiness(spam) > burstiness(variety) > 0


@pytest.mark.unit
def test_burstiness_normalizes_text_and_handles_small_samples() -> None:
    same = [ChatMessage(0, "a", "666"), ChatMessage(1, "b", " 666 "), ChatMessage(2, "c", "666")]
    assert burstiness(same) == pytest.approx(1.0)
    assert burstiness([]) == 0.0
    assert burstiness(_msgs([0.0], "1")) == 0.0  # 只有一条不构成刷屏
    assert distinct_ratio([]) == 1.0


@pytest.mark.unit
def test_messages_between_half_open() -> None:
    msgs = _msgs([1.0, 2.0, 3.0, 4.0])
    assert [m.t for m in messages_between(msgs, 2.0, 4.0)] == [2.0, 3.0]


# ------------------------------------------------------------------ 集成：stream_vod 弹幕


@pytest.mark.integration
def test_stream_vod_density_peak_is_the_lottery_decoy_but_burstiness_exposes_it(media, truth) -> None:
    """弹幕密度最高的窗口落在抽奖刷屏段（58–70s，是诱饵，画面音频平淡），
    但刷屏段几乎全是同一句"1"，而真高光 1、2 之后的弹幕是多样化的惊叹：
    仅看密度会选错，加上 burstiness 就能把它们分开。"""
    t = truth("stream_vod")
    msgs = load_chat(media / t["chat_file"])
    times, rate = chat_density(msgs, t["duration"], hop=1.0, window=5.0)
    peak = float(times[int(np.argmax(rate))])
    decoy = t["decoys"][0]
    assert decoy["start"] - 2 <= peak <= decoy["end"], f"密度峰 {peak}s 应在诱饵段内"

    decoy_msgs = messages_between(msgs, 58.0, 70.0)
    scores = {"decoy": burstiness(decoy_msgs)}
    for i in (0, 1):  # 高光 1、2（高光 3 本来就没有弹幕高峰）
        h = t["highlights"][i]
        scores[f"hl{i + 1}"] = burstiness(messages_between(msgs, h["start"], h["start"] + 13.0))
    log.info("burstiness=%s distinct_ratio decoy=%.3f", {k: round(v, 3) for k, v in scores.items()},
             distinct_ratio(decoy_msgs))
    assert scores["decoy"] > 0.8
    assert scores["hl1"] < 0.5 and scores["hl2"] < 0.5
    assert scores["decoy"] - max(scores["hl1"], scores["hl2"]) > 0.4  # 拉开足够大的差距
    # 用户给的口径：不同文本占比——诱饵远低于高光
    assert distinct_ratio(decoy_msgs) < 0.15
    for i in (0, 1):
        h = t["highlights"][i]
        assert distinct_ratio(messages_between(msgs, h["start"], h["start"] + 13.0)) > distinct_ratio(decoy_msgs) * 2


@pytest.mark.integration
def test_stream_vod_density_is_elevated_after_hype_highlights(media, truth) -> None:
    t = truth("stream_vod")
    msgs = load_chat(media / t["chat_file"])
    times, rate = chat_density(msgs, t["duration"])
    base = float(np.median(rate))
    for i in (0, 1):
        h = t["highlights"][i]
        sel = (times >= h["start"] + 2) & (times <= h["start"] + 12)
        assert rate[sel].max() > 3 * base
