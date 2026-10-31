"""CLI：transcript（分页读带句段 ID 的文字稿）与 clip（按 ID 新建候选 + 硬约束）。真 ASR + 合成素材（缺失时 skip）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autocut import cli

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def long_talk(tmp_path_factory: pytest.TempPathFactory) -> Path:
    media = Path(__file__).resolve().parent.parent / "fixtures" / "media" / "lecture_en.mp4"
    if not media.exists():
        pytest.skip("run fixtures/make_fixtures.py")
    d = tmp_path_factory.mktemp("clips")
    lst = d / "l.txt"
    lst.write_text("".join(f"file '{media}'\n" for _ in range(4)))
    out = d / "long.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)], check=True)
    return out


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = cli.main(list(argv))
    out, _ = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, out
    return code, json.loads(lines[0])


def test_transcript_pages_with_stable_ids(long_talk: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, r = run(capsys, "transcript", str(long_talk), "--limit", "5", "--lang", "en", "--json")
    assert code == 0 and r["ok"] and r["showing"] == 5 and r["offset"] == 0 and r["total"] >= 10
    assert [i["id"] for i in r["items"]] == ["s0001", "s0002", "s0003", "s0004", "s0005"]
    assert r["next_offset"] == 5 and any("--offset 5" in n["cmd"] for n in r["next"])
    _, r2 = run(capsys, "transcript", str(long_talk), "--offset", "5", "--limit", "5", "--lang", "en", "--json")
    assert r2["items"][0]["id"] == "s0006"  # id 跨页稳定
    _, r3 = run(capsys, "transcript", str(long_talk), "--around", "100", "--limit", "6", "--lang", "en", "--json")
    mid = [i for i in r3["items"] if i["start"] <= 100 <= i["end"] + 3]
    assert mid, r3["items"]


def test_transcript_limit_validated(long_talk: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, r = run(capsys, "transcript", str(long_talk), "--limit", "500", "--json")
    assert code == 2 and r["error"]["code"] == "usage" and "100" in r["error"]["message"]


def test_clip_creates_plan_and_enforces_constraints(long_talk: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "talk.autocut.json"
    _, tr = run(capsys, "transcript", str(long_talk), "--limit", "100", "--lang", "en", "--json")
    ids = [i["id"] for i in tr["items"]]
    # 太短：一句话不够 min 20s
    code, r = run(capsys, "clip", str(long_talk), "--from", ids[0], "--to", ids[0], "--title", "t", "--reason", "r", "--length", "20-60", "-o", str(out), "--lang", "en", "--json")
    assert code == 3 and r["error"]["code"] == "too_short" and "往后多包含几句" in r["error"]["fix"] and not out.exists()
    # 不存在的句段 id：不让模型自己编
    code, r = run(capsys, "clip", str(long_talk), "--from", ids[0], "--to", "s9999", "--title", "t", "--reason", "r", "-o", str(out), "--lang", "en", "--json")
    assert code == 3 and r["error"]["code"] == "unknown_sentence" and "时间戳" in r["error"]["fix"]
    # 合法：取前几句凑够 8–40s
    ok = None
    for j in range(1, 8):
        code, r = run(capsys, "clip", str(long_talk), "--from", ids[0], "--to", ids[j], "--title", "开头一段", "--reason", "测试", "--length", "8-40", "-o", str(out), "--lang", "en", "--json")
        if code == 0:
            ok = r
            break
    assert ok and ok["output"] == "clip_1" and ok["selected"] is False and out.exists() and 8 <= ok["duration"] <= 40
    assert any("autocut set" in n["cmd"] and "--select" in n["cmd"] for n in ok["next"])
    # 重叠：再选同一段 → 拒绝，方案不变
    rev = json.loads(out.read_text())["revision"]
    code, r = run(capsys, "clip", str(out), "--from", ids[0], "--to", ids[j], "--title", "again", "--reason", "r", "--length", "8-40", "--lang", "en", "--json")
    assert code == 3 and r["error"]["code"] == "overlaps_other" and json.loads(out.read_text())["revision"] == rev
    # --dry-run 不写
    code, r = run(capsys, "clip", str(out), "--from", ids[-6], "--to", ids[-1], "--title", "尾段", "--reason", "r", "--length", "5-60", "--dry-run", "--lang", "en", "--json")
    assert json.loads(out.read_text())["revision"] == rev
    # expect-revision 冲突
    code, r = run(capsys, "clip", str(out), "--from", ids[-6], "--to", ids[-1], "--title", "尾段", "--reason", "r", "--length", "5-60", "--expect-revision", "99", "--lang", "en", "--json")
    assert code == 3 and r["error"]["code"] == "revision_conflict"
    # 选中后能出片（竖屏），并被核验
    code, r = run(capsys, "set", str(out), "clip_1", "--select", "--json")
    assert code == 0
    code, r = run(capsys, "render", str(out), "--json")
    assert code == 0 and r["results"][0]["output"] == "clip_1" and r["results"][0]["verify"]["verified"] is True


def test_clip_rejects_non_clips_plan(long_talk: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "c.autocut.json"
    code, _ = run(capsys, "cut", str(long_talk), "-o", str(out), "--json")
    assert code == 0
    code, r = run(capsys, "clip", str(out), "--from", "s0001", "--to", "s0002", "--title", "t", "--reason", "r", "--json")
    assert code == 3 and r["error"]["code"] == "not_clips_plan"
