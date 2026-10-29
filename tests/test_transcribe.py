"""transcribe：whisper.cpp 封装（模型查找、JSON 解析与词边界吸附、缓存）。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace

import pytest

from autocut import transcribe as T
from autocut.analysis import Media

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ 合成数据


def _tok(text: str, t_dtw: int, p: float = 0.9) -> dict:
    return {"text": text, "t_dtw": t_dtw, "p": p, "offsets": {"from": 0, "to": 0}}


def _doc(*segments: list[dict]) -> dict:
    return {"result": {"language": "x"}, "transcription": [{"text": "", "tokens": toks} for toks in segments]}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tag", "preset"),
    [("large-v3-turbo", "large.v3.turbo"), ("large-v3-turbo-q5_0", "large.v3.turbo"), ("base", "base"),
     ("base.en", "base.en"), ("medium-q8_0", "medium"), ("large-v3", "large.v3"), ("my-finetune", None)],
)
def test_dtw_preset(tag: str, preset: str | None) -> None:
    assert T._dtw_preset(tag) == preset


@pytest.mark.unit
def test_english_subwords_merge_and_punct_split() -> None:
    """以空格开头的 token 才开始新词；标点单独成项、start == end == 前一个词的结束。"""
    data = _doc([_tok("[_BEG_]", -1), _tok(" Okay", 176), _tok(",", 218), _tok(" let", 240), _tok("'s", 250),
                 _tok(" get", 270), _tok(" g", 290), _tok("ulf", 300), _tok(".", 374), _tok("[_TT_272]", -1)])
    words, dropped = T.words_from_whisper(data, [(1.5, 2.04), (2.2, 3.2)])
    assert [w["text"] for w in words] == ["Okay", ",", "let's", "get", "gulf", "."]
    assert dropped == []
    okay, comma, lets, get, gulf, stop = words
    assert (okay["start"], okay["end"]) == (1.5, 2.04)  # 段内唯一的词占满语音段
    assert comma["start"] == comma["end"] == 2.04
    assert lets["start"] == 2.2  # 停顿之后的句首吸附到语音段起点，而不是上一个 token 的 t_dtw
    assert lets["end"] == 2.5 and get["start"] == 2.5  # 段内的词首尾相接
    assert gulf["end"] == 3.2  # 段内最后一个词延到语音段终点（t_dtw 偏早）
    assert stop["start"] == stop["end"] == 3.2


@pytest.mark.unit
def test_drops_words_in_silence() -> None:
    """结束时间附近没有语音能量的词（whisper 在空白处的幻觉）连同后面的标点一起丢掉。"""
    data = _doc([_tok(" Questions", 3412), _tok("?", 3454)],
                [_tok(" Thank", 3480 + 120), _tok(" you", 3500 + 150), _tok(".", 3700)])
    words, dropped = T.words_from_whisper(data, [(33.69, 34.33)])
    assert [w["text"] for w in words] == ["Questions", "?"]
    assert [d.split("@")[0] for d in dropped] == ["Thank", "you"]


@pytest.mark.unit
def test_early_dtw_word_goes_to_next_region() -> None:
    """t_dtw 偏早：落在下一个语音段起点前不远处的词归给下一个语音段。"""
    data = _doc([_tok("好", 100), _tok("，", 130), _tok("走", 180)])
    words, _ = T.words_from_whisper(data, [(0.5, 1.1), (2.0, 2.3)])
    zou = words[-1]
    assert zou["text"] == "走" and (zou["start"], zou["end"]) == (2.0, 2.3)


@pytest.mark.unit
def test_cjk_split_utf8_and_fullwidth_punct() -> None:
    """一个汉字被拆在两个 token 里时拼回来；中文后的半角逗号换成全角。"""
    b = "好".encode()
    part1, part2 = b[:2].decode("utf-8", "surrogateescape"), b[2:].decode("utf-8", "surrogateescape")
    data = _doc([_tok("大家", 40), _tok(part1, 50, 0.7), _tok(part2, 60, 0.8), _tok(",", 88), _tok("欢迎", 120)])
    words, _ = T.words_from_whisper(data, [(0.0, 0.7), (0.9, 1.3)])
    assert [w["text"] for w in words] == ["大家", "好", "，", "欢迎"]
    assert words[1]["p"] == 0.7  # 合并后取较低的概率


@pytest.mark.unit
def test_missing_dtw_falls_back_to_offsets() -> None:
    tok = {"text": " hi", "t_dtw": -1, "p": 0.9, "offsets": {"from": 0, "to": 1200}}
    words, _ = T.words_from_whisper(_doc([tok]), [(0.9, 1.5)])
    assert words == [{"text": "hi", "start": 0.9, "end": 1.5, "p": 0.9}]


# ------------------------------------------------------------------ 模型查找


@pytest.fixture
def model_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    repo, user = tmp_path / "repo-models", tmp_path / "user-models"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(T, "REPO_MODELS", repo)
    monkeypatch.setattr(T, "USER_MODELS", user)
    monkeypatch.delenv(T.ENV_MODEL, raising=False)
    return repo, user


@pytest.mark.unit
def test_find_model_prefers_f16_turbo(model_dirs: tuple[Path, Path]) -> None:
    repo, user = model_dirs
    for name in ("ggml-base.bin", "ggml-large-v3-turbo-q5_0.bin", "ggml-large-v3-turbo.bin", "ggml-silero-vad.bin"):
        (repo / name).write_bytes(b"x")
    assert T.find_model() == (repo / "ggml-large-v3-turbo.bin").resolve()
    assert T.model_tag() == "large-v3-turbo"


@pytest.mark.unit
def test_find_model_order(model_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """环境变量 → 仓库 models/ → ~/.cache/autocut/models/。"""
    repo, user = model_dirs
    (user / "ggml-base.bin").write_bytes(b"x")
    assert T.find_model() == (user / "ggml-base.bin").resolve()
    (repo / "ggml-small.bin").write_bytes(b"x")
    assert T.find_model() == (repo / "ggml-small.bin").resolve()
    custom = tmp_path / "custom.bin"
    custom.write_bytes(b"x")
    monkeypatch.setenv(T.ENV_MODEL, str(custom))
    assert T.find_model() == custom.resolve()
    assert T.model_tag() == "custom"
    monkeypatch.setenv(T.ENV_MODEL, str(tmp_path / "missing.bin"))  # 写错了：警告并继续找
    assert T.find_model() == (repo / "ggml-small.bin").resolve()


@pytest.mark.unit
def test_unavailable_reason_lists_search_paths(model_dirs: tuple[Path, Path]) -> None:
    repo, user = model_dirs
    assert T.find_model() is None
    assert not T.available()
    reason = T.unavailable_reason()
    assert reason is not None and "没有找到语音识别模型" in reason
    assert str(repo) in reason and str(user) in reason
    assert T.model_tag() == "none"


# ------------------------------------------------------------------ 缓存：原始 JSON 先落盘


@pytest.mark.unit
def test_reuses_cached_raw_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录里已有原始 JSON 时直接解析，不再跑 whisper-cli。"""
    model = tmp_path / "ggml-large-v3-turbo.bin"
    model.write_bytes(b"x")
    monkeypatch.setattr(T, "find_model", lambda: model)
    monkeypatch.setattr(T, "whisper_cli", lambda: "/bin/false")

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("不应重新识别")

    monkeypatch.setattr(T, "_run_whisper", boom)
    regions = ((0.2, 0.9),)
    media = SimpleNamespace(cache_dir=tmp_path, path=tmp_path / "x.mp4", duration=1.0,
                            speech=lambda track, **kw: SimpleNamespace(regions=regions),
                            track_label=lambda t: "默认音轨")
    (tmp_path / "whisper-zh-tNone-large-v3-turbo.json").write_text(json.dumps(_doc([_tok("你好", 60)])))
    words = T.transcribe(media, language="zh")  # type: ignore[arg-type]
    assert words == [{"text": "你好", "start": 0.2, "end": 0.9, "p": 0.9}]


# ------------------------------------------------------------------ 真实素材


needs_asr = pytest.mark.skipif(not T.available(), reason=T.unavailable_reason() or "")


@pytest.fixture(scope="module")
def asr_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """本次测试会话共用的分析缓存（与 test_recipe_cut 共享，避免同一素材识别两遍）。"""
    d = tmp_path_factory.getbasetemp() / "autocut-asr-cache"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AUTOCUT_CACHE", str(d))
        yield d


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


@pytest.mark.integration
@needs_asr
def test_talk_zh_words(media: Path, truth, asr_cache: Path) -> None:
    m = Media(media / "talk_zh.mp4")
    words = m.words()
    assert words, "talk_zh 应该识别出文字"
    # 原始 JSON 已落盘到缓存目录
    assert (m.cache_dir / f"whisper-auto-tNone-{T.model_tag()}.json").exists()
    assert m.words_cache_path() is not None

    # 词按时间递增，起止合法，标点 start == end
    for a, b in zip(words, words[1:]):
        assert a["start"] <= a["end"] <= b["start"] + 1e-6, (a, b)
    for w in words:
        assert 0 <= w["p"] <= 1
        if T._is_punct(w["text"]):
            assert w["start"] == w["end"]

    tr = truth("talk_zh")
    speech = [s for s in tr["segments"] if s["kind"] == "speech"]
    gaps = [s for s in tr["segments"] if s["kind"] == "gap"]
    # 每一句的文字和 truth 字符重合 ≥ 90%
    for s in speech:
        got = "".join(w["text"] for w in words if s["start"] - 0.15 <= (w["start"] + w["end"]) / 2 <= s["end"] + 0.15)
        ratio = SequenceMatcher(None, _norm(got), _norm(s["text"]), autojunk=False).ratio()
        log.info("%.2f %s | %s", ratio, got, s["text"])
        assert ratio >= 0.9, (s["text"], got)
    # 词的起止不落在 truth 的停顿中间（±150ms）
    for w in words:
        for g in gaps:
            for t in (w["start"], w["end"]):
                assert not (g["start"] + 0.15 < t < g["end"] - 0.15), (w, g)


@pytest.mark.integration
@needs_asr
def test_lecture_en_words_are_whole_words(media: Path, asr_cache: Path) -> None:
    words = Media(media / "lecture_en.mp4").words()
    texts = [w["text"] for w in words]
    assert "execution" in texts and "let's" in texts and "gulf" in texts
    assert all(not t.startswith(" ") for t in texts)
    assert not any(t in ("g", "ulf") for t in texts)  # 子词已合并
