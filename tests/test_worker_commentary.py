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
    cfg = WorkerConfig(root=tmp_path, tts_mini_url=f"http://127.0.0.1:{srv.server_address[1]}", tts_order=("mini",))
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








def test_tts_falls_back_to_system_voice_when_services_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mini TTS 连不上时降级到 macOS say——并且结果里记着 provider=say，调用方据此在摘要里标注降级。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    t = TTS(WorkerConfig(root=tmp_path, tts_mini_url="http://127.0.0.1:9"))
    r = t.synth_many(["你好世界"], voice="vivian", lang="Chinese")
    assert r[0][2] == "say" and r[0][1] > 0.3
    assert t.stats["provider"] == {"say": 1}




class FakeFeiyu(BaseHTTPRequestHandler):
    jobs: dict[str, int] = {}
    seen: list[dict[str, Any]] = []
    fail_first = False

    def log_message(self, *a: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        n = int(self.headers["Content-Length"])
        FakeFeiyu.seen.append({"auth": self.headers.get("Authorization"), **json.loads(self.rfile.read(n))})
        jid = f"j{len(FakeFeiyu.jobs) + 1}"
        FakeFeiyu.jobs[jid] = 0
        self._send(200, json.dumps({"id": jid, "status": "queued"}).encode())

    def do_GET(self) -> None:
        parts = self.path.strip("/").split("/")
        jid = parts[2]
        if parts[-1] == "audio":
            p = Path("/tmp") / f"fakefeiyu-{threading.get_ident()}.wav"
            _tone_wav(p, 1.5)
            return self._send(200, p.read_bytes(), "audio/wav")
        FakeFeiyu.jobs[jid] += 1
        self._send(200, json.dumps({"id": jid, "status": "running" if FakeFeiyu.jobs[jid] < 2 else "succeeded"}).encode())


def test_tts_feiyu_submit_poll_download_and_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeFeiyu.jobs, FakeFeiyu.seen = {}, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeFeiyu)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    sec = tmp_path / "w.env"
    sec.write_text("AUTOCUT_FEIYU_KEY=feiyu-secret\n")
    cfg = WorkerConfig(root=tmp_path, secrets_file=str(sec), tts_feiyu_url=f"http://127.0.0.1:{srv.server_address[1]}", tts_order=("feiyu", "say"))
    t = TTS(cfg)
    r = t.synth_many(["你好", "世界"], style="gentle")
    assert [x[2] for x in r] == ["feiyu", "feiyu"] and r[0][1] == pytest.approx(1.5, abs=0.02)
    assert FakeFeiyu.seen[0] == {"auth": "Bearer feiyu-secret", "text": "你好", "voice": "feiyu", "style": "gentle"}
    n = len(FakeFeiyu.seen)
    TTS(cfg).synth_many(["你好", "世界"], style="gentle")  # 重启后命中缓存：不再请求 laptop（不再加载 7.8GB 的模型）
    assert len(FakeFeiyu.seen) == n
    TTS(cfg).synth_many(["你好"], style="cute")  # 风格不同 = 不同缓存键
    assert len(FakeFeiyu.seen) == n + 1
    srv.shutdown()
    t2 = TTS(WorkerConfig(root=tmp_path / "x", secrets_file=str(sec), tts_feiyu_url="http://127.0.0.1:9", tts_order=("feiyu", "say")))
    assert t2.synth_many(["再见"])[0][2] == "say"  # laptop 不通（WSL 重启）：回落，不是失败
