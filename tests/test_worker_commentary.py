"""解说配音：纯逻辑（时间映射/排版/配音轨）、LLM 网关客户端（预算、先落盘再解析）、TTS（缓存/回落）、整条流水线（假 LLM/TTS + 真 autocut）。"""

from __future__ import annotations

import json
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from autocut.plan import Output, Segment
from autocut.worker import commentary as C
from autocut.worker.config import WorkerConfig
from autocut.worker.llm import BudgetExceeded, Gateway, LLMError, extract_json
from autocut.worker.tts import TTS, TTSError, wav_duration

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ 纯逻辑


def _out() -> Output:
    return Output("main", [Segment("a", "v", 10.0, 20.0), Segment("b", "v", 20.0, 25.0, action="cut"), Segment("c", "v", 25.0, 40.0)])


def test_out_to_src_skips_cut_parts() -> None:
    o = _out()  # 输出时间轴：a 0–10（素材 10–20），c 10–25（素材 25–40）
    assert C.out_to_src(o, 3.0) == ("v", 13.0)
    assert C.out_to_src(o, 10.0) == ("v", 25.0)
    assert C.out_to_src(o, 24.99)[1] == pytest.approx(39.99, abs=0.02)
    s = C.out_to_src(o, 25.0)
    assert s is not None and s[1] <= 40.0


def test_frame_times_even_and_capped() -> None:
    ts = C.frame_times(120.0, n_max=40)
    assert len(ts) == 30 and ts[0] == pytest.approx(2.0) and ts[-1] < 120
    assert len(C.frame_times(1000.0, n_max=40)) == 40
    assert C.frame_times(0) == []


def test_extract_json_tolerates_fences_and_noise() -> None:
    assert extract_json('好的：\n```json\n{"a": 1}\n```\n以上') == {"a": 1}
    assert extract_json('结果 [1,2,3] 完') == [1, 2, 3]
    with pytest.raises(ValueError):
        extract_json("没有 json")


def test_validate_script_rules() -> None:
    v = C.validate_script(60.0, 8)
    v({"lines": [{"at": 1, "text": "嗨"}]})
    for bad in ({"lines": []}, {"lines": [{"at": 61, "text": "x"}]}, {"lines": [{"at": 1, "text": " "}]}, {"lines": [{"at": 1, "text": "x"}] * 12}):
        with pytest.raises((ValueError, KeyError)):
            v(bad)


def test_clean_lines_sorts_and_merges_close_starts() -> None:
    ls = C.clean_lines([{"at": 10, "text": "b"}, {"at": 2, "text": "a"}, {"at": 10.4, "text": "c"}], 60)
    assert [(x.at, x.text) for x in ls] == [(2.0, "a"), (10.0, "b")]


def test_fit_speeds_up_then_flags_too_long() -> None:
    ls = [C.Line(0.0, "a", dur=3.0), C.Line(4.0, "b", dur=3.0), C.Line(8.0, "c", dur=5.0)]
    ls[0].dur, ls[1].dur = 3.0, 4.0  # 槽位 3.75、3.75
    bad = C.fit(ls, 20.0)
    assert ls[0].speed == 1.0 and 1.0 < ls[1].speed <= 1.2 and bad == []
    ls[1].dur = 6.0
    assert C.fit(ls, 20.0) == [1]
    assert C.max_chars_for(2.0) == int(2.0 * C.CHARS_PER_SEC * 1.2)


def _tone_wav(path: Path, secs: float, sr: int = 24000, f: float = 600.0) -> Path:
    t = np.arange(int(secs * sr)) / sr
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((0.3 * np.sin(2 * np.pi * f * t) * 32767).astype("<i2").tobytes())
    return path


def test_build_track_places_lines_at_times(tmp_path: Path) -> None:
    a, b = _tone_wav(tmp_path / "a.wav", 1.0), _tone_wav(tmp_path / "b.wav", 1.0, f=900)
    ls = [C.Line(1.0, "第一句", wav=a, dur=1.0), C.Line(4.0, "第二句", wav=b, dur=1.0, speed=1.25), C.Line(6.0, "丢", wav=a, dur=1, dropped="x")]
    dst = tmp_path / "n.wav"
    words = C.build_track(ls, 8.0, dst)
    assert [w["text"] for w in words] == ["第一句", "第二句"] and words[0]["start"] == 1.0
    assert wav_duration(dst) == pytest.approx(8.0, abs=0.01)
    x = np.frombuffer(wave.open(str(dst), "rb").readframes(8 * C.SR), dtype="<i2").astype(float) / 32767
    def rms(a0: float, a1: float) -> float:
        return float(np.sqrt(np.mean(x[int(a0 * C.SR):int(a1 * C.SR)] ** 2)))
    assert rms(1.1, 1.9) > 0.1 and rms(2.5, 3.5) < 0.01 and rms(4.1, 4.7) > 0.1 and rms(6.2, 7.5) < 0.01  # 第二句加速后 0.8s


def test_speech_intervals_merge_close_words() -> None:
    o = Output("main", [Segment("a", "v", 0.0, 30.0)])
    words = [{"text": "你好", "start": 1.0, "end": 1.5}, {"text": "世界", "start": 1.8, "end": 2.2}, {"text": "再见", "start": 9.0, "end": 9.4}]
    sp = C.speech_intervals(words, o)
    assert len(sp) == 2 and sp[0]["start"] == pytest.approx(1.25) and "世界" in sp[0]["text"]


# ------------------------------------------------------------------ LLM 网关（本地假网关）


class FakeGateway(BaseHTTPRequestHandler):
    spend = 0.0
    replies: list[str] = []
    seen: list[dict[str, Any]] = []

    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:  # /api/me
        body = json.dumps({"spend_usd": FakeGateway.spend}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        n = int(self.headers["Content-Length"])
        req = json.loads(self.rfile.read(n))
        FakeGateway.seen.append(req)
        FakeGateway.spend += 0.05  # 每次调用 $0.05（网关差值）
        text = FakeGateway.replies.pop(0) if FakeGateway.replies else "{}"
        body = json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"total_tokens": 100}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_gw(tmp_path: Path) -> Any:
    FakeGateway.spend, FakeGateway.replies, FakeGateway.seen = 0.0, [], []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    secrets = tmp_path / "w.env"
    secrets.write_text("AUTOCUT_LLM_KEY=llmgw_test\n")
    cfg = WorkerConfig(root=tmp_path, llm_base=f"http://127.0.0.1:{srv.server_address[1]}", secrets_file=str(secrets))
    yield cfg
    srv.shutdown()


def test_gateway_saves_raw_before_parse_and_tracks_spend(fake_gw: WorkerConfig, tmp_path: Path) -> None:
    jd = tmp_path / "job"
    gw = Gateway(fake_gw, jd, budget_cny=2.0)
    FakeGateway.replies = ['```json\n{"ok": true}\n```']
    obj = gw.chat_json("m", [{"role": "user", "content": "hi"}], purpose="t")
    assert obj == {"ok": True}
    raw = next((jd / "work" / "llm").glob("001-t.json"))
    assert "choices" in json.loads(raw.read_text())
    assert gw.spent_usd() == pytest.approx(0.05) and gw.spent_cny() == pytest.approx(0.36)
    assert json.loads((jd / "work" / "budget.json").read_text())["by_purpose"]["t"] == pytest.approx(0.05)


def test_gateway_bad_json_retries_once_then_fails(fake_gw: WorkerConfig, tmp_path: Path) -> None:
    gw = Gateway(fake_gw, tmp_path / "job", 2.0)
    FakeGateway.replies = ["不是 json", '{"fixed": 1}']
    assert gw.chat_json("m", [{"role": "user", "content": "x"}], purpose="t") == {"fixed": 1}
    assert "不能用" in FakeGateway.seen[-1]["messages"][-1]["content"]
    FakeGateway.replies = ["坏", "还是坏"]
    with pytest.raises(LLMError) as e:
        gw.chat_json("m", [{"role": "user", "content": "x"}], purpose="t2")
    assert e.value.code == "llm_bad_json"


def test_gateway_budget_hard_stop(fake_gw: WorkerConfig, tmp_path: Path) -> None:
    gw = Gateway(fake_gw, tmp_path / "job", budget_cny=0.5)  # ≈ $0.069，两次调用 $0.10 就超
    FakeGateway.replies = ["{}", "{}", "{}"]
    gw.chat("m", [{"role": "user", "content": "1"}], purpose="a")
    gw.chat("m", [{"role": "user", "content": "2"}], purpose="b")
    n = len(FakeGateway.seen)
    with pytest.raises(BudgetExceeded) as e:
        gw.chat("m", [{"role": "user", "content": "3"}], purpose="c")
    assert len(FakeGateway.seen) == n and e.value.fix  # 超限后一次请求都不再发


def test_gateway_without_key_says_how_to_fix(tmp_path: Path) -> None:
    gw = Gateway(WorkerConfig(root=tmp_path), tmp_path / "job", 2.0)
    with pytest.raises(LLMError) as e:
        gw.chat("m", [], purpose="t")
    assert e.value.code == "no_llm_key" and "AUTOCUT_LLM_KEY" in e.value.fix


# ------------------------------------------------------------------ TTS（本地假 mini TTS）


class FakeTTS(BaseHTTPRequestHandler):
    calls = 0
    fail = False

    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers["Content-Length"])
        self.rfile.read(n)
        FakeTTS.calls += 1
        if FakeTTS.fail:
            self.send_response(500)
            self.end_headers()
            return
        p = Path("/tmp") / f"faketts-{threading.get_ident()}.wav"
        _tone_wav(p, 1.0)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_tts_caches_by_content_and_survives_restart(tmp_path: Path) -> None:
    FakeTTS.calls, FakeTTS.fail = 0, False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTTS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = WorkerConfig(root=tmp_path, tts_mini_url=f"http://127.0.0.1:{srv.server_address[1]}")
    t1 = TTS(cfg)
    r = t1.synth_many(["你好", "世界", "你好"])
    assert FakeTTS.calls == 2 and r[0][0] == r[2][0] and r[0][1] == pytest.approx(1.0, abs=0.01) and r[0][2] == "mini"
    t2 = TTS(cfg)  # 新实例 = worker 重启：缓存在磁盘上
    t2.synth_many(["你好", "世界"])
    assert FakeTTS.calls == 2 and t2.stats["hit"] == 2
    srv.shutdown()


def test_tts_failure_is_reported_not_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTTS.calls, FakeTTS.fail = 0, True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTTS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    t = TTS(WorkerConfig(root=tmp_path, tts_mini_url=f"http://127.0.0.1:{srv.server_address[1]}", tts_order=("mini",)))
    with pytest.raises(TTSError):
        t.synth_many(["a", "b", "c"])
    srv.shutdown()


def test_spend_uses_max_of_gateway_delta_and_token_estimate(fake_gw: WorkerConfig, tmp_path: Path) -> None:
    """网关 spend 结算有延迟（差值常为 0）：花费取 max(差值, 按 token 的估价)，预算不能被低估。"""
    from autocut.worker.llm import estimate_usd

    assert estimate_usd("claude-sonnet-5-5", {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}) == pytest.approx(4.5)
    assert estimate_usd("unknown-model", {"total_tokens": 1_000_000}) == pytest.approx(3.0)
    gw = Gateway(fake_gw, tmp_path / "job", 100.0)
    FakeGateway.spend = 0.0
    orig = FakeGateway.do_POST

    def no_spend(self):  # 模拟结算延迟：差值为 0
        orig(self)
        FakeGateway.spend = 0.0

    FakeGateway.do_POST = no_spend  # type: ignore[method-assign]
    try:
        FakeGateway.replies = ["{}"]
        r = gw.chat("claude-sonnet-5-5", [{"role": "user", "content": "x"}], purpose="t")
    finally:
        FakeGateway.do_POST = orig  # type: ignore[method-assign]
    assert r["usd"] > 0 and gw.spent_usd() == pytest.approx(r["usd"])


def test_claude_request_params(fake_gw: WorkerConfig, tmp_path: Path) -> None:
    """Claude 5 系列：不传 temperature；sonnet/opus 关掉预思考（否则正文为空）；haiku 不能带 thinking 参数。"""
    gw = Gateway(fake_gw, tmp_path / "job", 100.0)
    FakeGateway.replies = ["{}", "{}", "{}"]
    gw.chat("claude-sonnet-5-5", [{"role": "user", "content": "x"}], purpose="a", temperature=0.7)
    gw.chat("claude-haiku-5-5", [{"role": "user", "content": "x"}], purpose="b", temperature=0.7)
    gw.chat("deepseek-flash", [{"role": "user", "content": "x"}], purpose="c", temperature=0.7)
    s, h, d = FakeGateway.seen[-3:]
    assert "temperature" not in s and s["thinking"] == {"type": "between_tools"}
    assert "temperature" not in h and "thinking" not in h
    assert d["temperature"] == 0.7 and "thinking" not in d


def test_tts_falls_back_to_system_voice_when_services_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mini TTS 连不上时降级到 macOS say——并且结果里记着 provider=say，调用方据此在摘要里标注降级。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    t = TTS(WorkerConfig(root=tmp_path, tts_mini_url="http://127.0.0.1:9"))
    r = t.synth_many(["你好世界"], voice="vivian", lang="Chinese")
    assert r[0][2] == "say" and r[0][1] > 0.3
    assert t.stats["provider"] == {"say": 1}
