"""字幕：切掉一段后时间平移、被剪掉的词不出现、断行不超过 max_chars、每条至少 0.8s、SRT 往返。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocut.captions import MIN_CUE, Cue, _wlen, cues_for, format_srt, parse_srt, write_srt
from autocut.plan import Bed, Captions, Output, Plan, Segment, Source

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"
_PUN = set("，。！？、；：,.!?")


def words_from_truth(truth: dict) -> list[dict]:
    """用 truth.json 的逐句文本合成逐字时间戳（句内字均分，标点 start==end），测试不依赖语音识别模型。"""
    words: list[dict] = []
    for t in truth["segments"]:
        if t["kind"] != "speech":
            continue
        chars = list(t["text"])
        n = sum(1 for c in chars if c not in _PUN)
        dt = (t["end"] - t["start"]) / n
        cur = t["start"]
        for c in chars:
            if c in _PUN:
                words.append({"text": c, "start": round(cur, 4), "end": round(cur, 4), "p": 1.0})
            else:
                words.append({"text": c, "start": round(cur, 4), "end": round(cur + dt, 4), "p": 1.0})
                cur += dt
    return words


def _plan(tmp_path: Path, words: list[dict], segs: list[Segment], *, max_chars: int = 16, dur: float = 60.0, **out_kw) -> tuple[Plan, Output]:
    wp = tmp_path / "words.json"
    wp.write_text(json.dumps(words, ensure_ascii=False))
    out = Output(id="main", segments=segs, captions=Captions(enabled=True, max_chars=max_chars), **out_kw)
    plan = Plan(recipe="manual", sources={"a": Source(id="a", path="/nonexistent/a.mp4", duration=dur, words=str(wp))}, outputs=[out],
                path=tmp_path / "x.autocut.json")
    return plan, out


def _sentence(text: str, start: float, step: float = 0.25) -> list[dict]:
    """每个字 step 秒；标点 start==end。"""
    out, t = [], start
    for c in text:
        if c in _PUN:
            out.append({"text": c, "start": t, "end": t, "p": 1.0})
        else:
            out.append({"text": c, "start": round(t, 4), "end": round(t + step, 4), "p": 1.0})
            t += step
    return out


def _seg(sid: str, a: float, b: float, **kw) -> Segment:
    return Segment(id=sid, source="a", start=a, end=b, reason="speech", **kw)


@pytest.mark.unit
def test_cut_in_the_middle_shifts_later_cues_and_drops_cut_words(tmp_path: Path) -> None:
    words = _sentence("大家好，欢迎收看。", 0.0) + _sentence("这句话被剪掉了。", 4.0) + _sentence("我们下期再见。", 8.0)
    segs = [_seg("a", 0, 3.0), _seg("b", 3.0, 7.0, action="cut"), _seg("c", 7.0, 11.0)]
    plan, out = _plan(tmp_path, words, segs)
    cues = cues_for(plan, out)
    text = "".join(c.text for c in cues)
    assert "剪掉" not in text and "被" not in text
    assert [c.text for c in cues] == ["大家好 欢迎收看", "我们下期再见"]
    # 第二句素材时间 8.0 起，剪掉 4s（3.0–7.0）→ 输出时间 4.0
    assert cues[1].start == pytest.approx(4.0, abs=0.02)
    assert cues[0].start == pytest.approx(0.0, abs=0.02)


@pytest.mark.unit
def test_speed_and_slot_are_mapped(tmp_path: Path) -> None:
    words = _sentence("一二三四五六七八。", 0.0, 0.25)  # 2 秒
    plan, out = _plan(tmp_path, words, [_seg("a", 0, 2.0, speed=2.0)])
    c = cues_for(plan, out)[0]
    assert c.start == pytest.approx(0.0, abs=0.02)
    assert c.end <= out.duration + 1e-6 and out.duration == pytest.approx(1.0)
    # slot 截断：只留前 1 秒，超出的词不出现
    plan2, out2 = _plan(tmp_path, words, [Segment(id="a", source="a", start=0, end=2.0, slot=1.0)])
    t = "".join(c.text for c in cues_for(plan2, out2))
    assert t.startswith("一二三四") and "八" not in t


@pytest.mark.unit
def test_reversed_or_muted_segments_have_no_captions(tmp_path: Path) -> None:
    words = _sentence("你好世界。", 0.0)
    plan, out = _plan(tmp_path, words, [_seg("a", 0, 1.2, mute=True)])
    assert cues_for(plan, out) == []
    plan, out = _plan(tmp_path, words, [_seg("a", 0, 1.2, reverse=True)])
    assert cues_for(plan, out) == []


@pytest.mark.unit
def test_long_sentence_is_split_within_max_chars_and_balanced(tmp_path: Path) -> None:
    text = "今天我们来聊一聊，怎么把一段录屏快速剪成可以发布的视频，而且不需要任何剪辑经验。"
    words = _sentence(text, 0.0, 0.2)
    plan, out = _plan(tmp_path, words, [_seg("a", 0, 8.0)], max_chars=12)
    cues = cues_for(plan, out)
    assert len(cues) >= 3
    for c in cues:
        assert _wlen(c.text) <= 12, c
    # 不能出现只剩一两个字的孤行
    assert min(_wlen(c.text) for c in cues) >= 4
    # 内容没丢（去掉标点和空格后与原文一致）
    assert "".join(c.text.replace(" ", "") for c in cues) == "".join(ch for ch in text if ch not in _PUN)


@pytest.mark.unit
def test_sentence_level_words_are_split_like_word_level(tmp_path: Path) -> None:
    """粗粒度文字稿（一项一整句，例如 narrate 的配音稿）也要能按标点和 max_chars 断行，时间按字数比例分配。"""
    zh = "今天我们来聊一聊，怎么把一段录屏快速剪成可以发布的视频，而且不需要任何剪辑经验。"
    en = "We cover the gulf of execution, and the gulf of evaluation."
    words = [{"text": zh, "start": 0.0, "end": 9.0, "p": 1.0}, {"text": en, "start": 10.0, "end": 14.0, "p": 1.0}]
    plan, out = _plan(tmp_path, words, [_seg("a", 0, 14.5)], max_chars=12)
    cues = cues_for(plan, out)
    zh_cues = [c for c in cues if c.end <= 9.5]
    assert len(zh_cues) >= 3 and all(_wlen(c.text) <= 12 for c in cues)
    assert "".join(c.text.replace(" ", "") for c in zh_cues) == "".join(ch for ch in zh if ch not in _PUN)
    # 时间按字数比例：第一条字幕不会占满整句的 9 秒
    assert zh_cues[0].end - zh_cues[0].start < 4.5
    en_text = " ".join(c.text for c in cues if c.start >= 9.9)
    assert en_text.startswith("We cover the gulf") and "  " not in en_text


@pytest.mark.unit
def test_english_uses_double_chars_and_spaces(tmp_path: Path) -> None:
    ws = "Today we cover the gulf of execution and the gulf of evaluation".split()
    words, t = [], 0.0
    for w in ws:
        words.append({"text": w, "start": t, "end": t + 0.3, "p": 1.0})
        t += 0.35
    words.append({"text": ".", "start": t, "end": t, "p": 1.0})
    plan, out = _plan(tmp_path, words, [_seg("a", 0, 5.0)], max_chars=16)  # 英文上限 32 字符
    cues = cues_for(plan, out)
    assert len(cues) == 3  # 62 个字符、每行 ≤32：两行装不下
    for c in cues:
        assert len(c.text) <= 32 and "  " not in c.text
    assert " ".join(c.text for c in cues).startswith("Today we cover")


@pytest.mark.unit
def test_min_duration_and_no_overlap(tmp_path: Path) -> None:
    # 一连串很短的句子：每条都不到 0.8s，应延长到 0.8s 或并入相邻
    words = []
    for i in range(6):
        words += _sentence("你好吗。", i * 0.5, 0.1)
    words += _sentence("嗯。", 4.0, 0.1) + _sentence("这是一句足够长的话，说完之后停一会儿。", 6.0, 0.2)
    plan, out = _plan(tmp_path, words, [_seg("a", 0, 12.0)])
    cues = cues_for(plan, out)
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start + 1e-6, (a, b)
    last = cues[-1]
    assert last.end - last.start >= MIN_CUE - 1e-6
    # 孤立的短句（后面有足够空隙）显示满 0.8s
    solo = [c for c in cues if c.text.startswith("这是一句")][0]
    assert solo.end - solo.start >= MIN_CUE


@pytest.mark.unit
def test_cut_seam_breaks_a_cue(tmp_path: Path) -> None:
    """一句话被剪口劈成两半（中间被剪掉很长一段），不硬连成一条。"""
    words = _sentence("我觉得这个", 0.0, 0.2) + _sentence("方法很好用", 10.0, 0.2)
    segs = [_seg("a", 0, 1.1), _seg("b", 1.1, 9.9, action="cut"), _seg("c", 9.9, 11.5)]
    plan, out = _plan(tmp_path, words, segs)
    cues = cues_for(plan, out)
    assert [c.text for c in cues] == ["我觉得这个", "方法很好用"]


@pytest.mark.unit
def test_narration_bed_words_follow_the_bed(tmp_path: Path) -> None:
    """captions.source 指向配音素材：词按 bed 的 at/start 映射。"""
    wp = tmp_path / "n.json"
    wp.write_text(json.dumps(_sentence("为什么天空是蓝色的？", 1.0, 0.2), ensure_ascii=False))
    plan = Plan(
        recipe="manual",
        sources={"img": Source(id="img", path="/x.png", kind="image"), "n": Source(id="n", path="/x.wav", kind="audio", duration=10.0, words=str(wp))},
        outputs=[Output(id="o", segments=[Segment(id="i", source="img", start=0, end=0, slot=6.0, reason="image")],
                        beds=[Bed(source="n", at=0.5, start=0.0, end=6.0, role="narration")],
                        captions=Captions(enabled=True, source="n"))],
        path=tmp_path / "p.autocut.json",
    )
    cues = cues_for(plan, plan.outputs[0])
    assert len(cues) == 1 and cues[0].text == "为什么天空是蓝色的？"
    assert cues[0].start == pytest.approx(1.5, abs=0.02)  # 素材 1.0s + bed.at 0.5s


@pytest.mark.unit
def test_srt_roundtrip(tmp_path: Path) -> None:
    cues = [Cue(0.0, 1.5, "你好"), Cue(3723.456, 3725.0, "第二行\n换行")]
    p = write_srt(cues, tmp_path / "sub" / "a.srt")
    txt = p.read_text(encoding="utf-8")
    assert txt.startswith("1\n00:00:00,000 --> 00:00:01,500\n你好\n")
    assert "01:02:03,456 --> 01:02:05,000" in txt
    back = parse_srt(txt)
    assert [(c.text, round(c.start, 3), round(c.end, 3)) for c in back] == [(c.text, c.start, c.end) for c in cues]
    assert format_srt([]) == ""


@pytest.mark.integration
def test_talk_zh_cut_a_sentence_shifts_following_captions(tmp_path: Path, truth) -> None:
    """用 talk_zh 的 truth 合成文字稿：剪掉重说那两句，后面的字幕提前对应的时长，被剪掉的字不出现。"""
    t = truth("talk_zh")
    words = words_from_truth(t)
    (tmp_path / "w.json").write_text(json.dumps(words, ensure_ascii=False))
    segs, removed = [], 0.0
    kept_starts: dict[str, float] = {}
    for i, s in enumerate(t["segments"]):
        if s["kind"] != "speech":
            continue
        cut = bool(s.get("tag"))
        segs.append(Segment(id=f"s{i:02d}", source="a", start=s["start"], end=s["end"], action="cut" if cut else "keep", reason="retake" if cut else "speech"))
    plan = Plan(recipe="manual", sources={"a": Source(id="a", path=str(MEDIA / "talk_zh.mp4"), duration=t["duration"], words=str(tmp_path / "w.json"))},
                outputs=[Output(id="o", segments=segs, captions=Captions(enabled=True))], path=tmp_path / "t.autocut.json")
    out = plan.outputs[0]
    cues = cues_for(plan, out)
    joined = "".join(c.text for c in cues).replace(" ", "")
    for s in t["segments"]:
        if s["kind"] == "speech" and s.get("tag") in ("retake", "filler"):
            body = "".join(ch for ch in s["text"] if ch not in _PUN)
            assert body not in joined, s["text"]
    # 最后一句「我们下期再见」：素材 38.18s 起，输出时间 = 素材时间 - 之前所有被剪掉的时长
    tl = {sg.id: (o0, o1) for sg, o0, o1 in out.timeline()}
    last_seg = [sg for sg in out.kept()][-1]
    last_cue = cues[-1]
    assert last_cue.text.replace(" ", "") == "我们下期再见"
    assert last_cue.start == pytest.approx(tl[last_seg.id][0], abs=0.05)
    for c in cues:
        assert 0 <= c.start < c.end <= out.duration + 1e-6
        assert _wlen(c.text) <= out.captions.max_chars
