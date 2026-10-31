"""cli：人和 agent 共用的命令行。

验证的是接口契约，而不是配方本身的剪辑质量（那些在 test_recipe_*）：
- 输出：非 TTY 自动 JSON，stdout 只有一个对象；TTY 时给人看的摘要；NO_COLOR。
- 退出码：2 用法错误、3 输入错误、4 缺依赖；异常不把 traceback 打到 stdout。
- 安全：--dry-run 不写文件；render/export 不覆盖已有文件；set 幂等、--expect-revision 冲突。
- 有界：show 分页（total/showing/next_offset/notice）、uncertain 最多 10 条。
- 不静默降级：没有语音识别时 notes 写原因和补法、summary.degraded=true。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from autocut import cli
from autocut.plan import Output, Plan, Segment, Source

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "fixtures" / "media"
AUTOCUT_BIN = ROOT / ".venv" / "bin" / "autocut"
JSON_BUDGET = 14_000  # 约 4k token（中文 UTF-8 每字 3 字节、约 1 token）


# ------------------------------------------------------------------ 工具


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    """在进程内跑 main()；断言 stdout 恰好是一个 JSON 对象。"""
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"stdout 应该只有一个 JSON 对象，实际：\n{out}\nstderr：\n{err}"
    res = json.loads(lines[0])
    if res["ok"]:
        assert res["command"] == argv[0]
        assert list(res)[-2:] == ["next", "notes"], list(res)
        for n in res["next"]:
            assert set(n) == {"cmd", "why"} and n["cmd"] and n["why"]
    else:
        assert set(res["error"]) == {"code", "message", "fix", "retry"}, res
        assert res["error"]["message"] and res["error"]["fix"]
    assert "Traceback" not in out
    return code, res, err


def force_tty(monkeypatch: pytest.MonkeyPatch, stdout: bool = True) -> None:
    """让 cli 以为 stdout 是终端（stderr 保持非终端，不画动画）。"""
    monkeypatch.setattr(cli, "_isatty", lambda s: stdout and s is sys.stdout)


def synthetic_plan(tmp_path: Path, n: int = 120, name: str = "talk.autocut.json") -> Path:
    """n 个 1 秒的片段：每 3 个剪 1 个（长停顿），每 10 个有 1 个拿不准（把握 0.6）。不需要素材文件。"""
    segs = []
    for i in range(n):
        cut = i % 3 == 0
        segs.append(Segment(
            id=f"d{i + 1:03d}", source="talk", start=float(i), end=float(i + 1), action="cut" if cut else "keep",
            reason="silence" if cut else "speech", confidence=0.6 if i % 10 == 5 else 0.95,
            detail="停顿 1.0s → 保留 0.4s" if cut else "说话 1.0s", text="" if cut else f"第 {i} 句话",
        ))
    plan = Plan(recipe="cut", sources={"talk": Source(id="talk", path=str(MEDIA / "talk_zh.mp4"), duration=n + 1.0)},
                outputs=[Output("main", segments=segs)], options={"retakes": True, "fillers": True})
    return plan.save(tmp_path / name)


def multi_output_plan(tmp_path: Path) -> Path:
    """高光式的方案：三条输出，片段 id 在每条里都是 c001、c002。"""
    outs = []
    for k in range(3):
        segs = [Segment(id="c001", source="vod", start=10.0 * k, end=10.0 * k + 2, reason="lead_in", confidence=0.85),
                Segment(id="c002", source="vod", start=10.0 * k + 2, end=10.0 * k + 8, reason="highlight", confidence=0.6 if k == 2 else 0.9)]
        outs.append(Output(f"clip_{k + 1}", segments=segs, selected=k < 2, score=90 - k * 10, title=f"第 {k + 1} 条",
                           reasons=[f"音量峰 +{15 - k}dB"]))
    plan = Plan(recipe="highlights", sources={"vod": Source(id="vod", path=str(MEDIA / "stream_vod.mp4"), duration=150.0)},
                outputs=outs)
    return plan.save(tmp_path / "vod.autocut.json")


def needs_media() -> None:
    if not (MEDIA / "talk_zh.mp4").exists():
        pytest.skip("缺少 fixtures/media：先运行 fixtures/make_fixtures.py")


@pytest.fixture(scope="module")
def asr_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """和 test_recipe_cut / test_transcribe 共用的分析缓存（同一素材只识别一遍），不往 fixtures/ 里写。"""
    d = tmp_path_factory.getbasetemp() / "autocut-asr-cache"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AUTOCUT_CACHE", str(d))
        yield d


# ================================================================== 单元：解析、帮助、输出契约


@pytest.mark.unit
def test_parse_helpers() -> None:
    assert cli.parse_time("61.5") == 61.5
    assert cli.parse_time("1:01.5") == pytest.approx(61.5)
    assert cli.parse_time("1:00:02") == 3602
    assert cli.parse_range("61.5-75") == (61.5, 75.0)
    assert cli.parse_range("1:01.5-1:15") == pytest.approx((61.5, 75.0))
    assert cli.parse_length_range("15-30") == (15.0, 30.0)
    assert cli.parse_length_range("30") == (15.0, 30.0)
    assert cli.parse_pace("Relaxed") == "relaxed"
    assert cli.parse_pace("0.3") == 0.3
    assert cli.parse_loudnorm("off") is False and cli.parse_loudnorm("-16") == -16.0 and cli.parse_loudnorm("auto") is None
    assert cli.parse_size("1080x1920") == (1080, 1920)
    for bad, fn in [("abc", cli.parse_time), ("75-61", cli.parse_range), ("fast", cli.parse_pace), ("11", cli.parse_pace),
                    ("loud", cli.parse_loudnorm), ("1920", cli.parse_size)]:
        with pytest.raises(Exception):
            fn(bad)


@pytest.mark.unit
def test_help_values_come_from_code(capsys: pytest.CaptureFixture[str]) -> None:
    """help 里的数值从代码里取（pace 各档秒数、拿不准的阈值、导出格式），不写死。"""
    from autocut import export
    from autocut.plan import UNCERTAIN_BELOW
    from autocut.recipes.cut import PACES

    assert set(cli.EXPORT_TARGETS) == set(export.FORMATS)
    assert cli.main(["cut", "--help"]) == 0
    cut_help = " ".join(capsys.readouterr().out.split())
    for name, sec in PACES.items():
        assert f"{name} {sec:g}s" in cut_help
    assert f"把握度低于 {UNCERTAIN_BELOW:g} 的算拿不准" in cut_help and "以返回里的 plan 字段为准" in cut_help
    assert cli.main(["export", "--help"]) == 0
    exp = " ".join(capsys.readouterr().out.split())
    assert "fcpxml（Final Cut Pro、DaVinci Resolve）" in exp and "xmeml（Premiere Pro）" in exp and "edl（通用格式" in exp
    assert "verified_in_nle" in exp and "tell_user" in exp
    assert cli.main(["render", "--help"]) == 0
    ren = " ".join(capsys.readouterr().out.split())
    assert all(w in ren for w in ("时长", "音画漂移", "响度", "接缝是否落在说话中间", "pid=N", "--no-verify"))
    assert cli.main(["highlights", "--help"]) == 0
    hl = " ".join(capsys.readouterr().out.split())
    assert "9:16" in hl and "字幕" in hl and "抽奖刷屏" in hl and '{"t": 秒, "text": "内容"}' in hl and "可选" in hl
    for cmd in ("doctor", "transcribe"):
        assert cli.main([cmd, "--help"]) == 0
        assert "不要自己在磁盘上找模型" in capsys.readouterr().out
    assert cli.main(["review", "--help"]) == 0
    assert "--detach" in capsys.readouterr().out


@pytest.mark.unit
def test_help_uses_card_description(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["cut", "--help"]) == 0
    out = capsys.readouterr().out
    assert "给口播、录课、游戏实况做剪辑方案：剪掉长停顿、重说的段落和口头禅" in out
    assert "**不适用**" in out and "--pace" in out
    assert cli.main(["--help"]) == 0
    top = capsys.readouterr().out
    assert "全局约定" in top and "退出码" in top
    for name in cli.COMMANDS:
        assert name in top
        assert f"{name}.short" in cli.HELP and name in cli.HELP


@pytest.mark.unit
@pytest.mark.parametrize("argv", [["cut"], ["cutt", "x.mp4"], ["cut", "x.mp4", "--pace", "fast"], ["export", "p.json", "--format", "mov"],
                                  ["show", "p.json", "--filter", "nope"], ["montage", "a.mp4"]])
def test_usage_errors_are_json_exit_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    code, res, _ = run(capsys, *argv)
    assert code == 2 and res["error"]["code"] == "usage"
    assert "--help" in res["error"]["fix"]


@pytest.mark.unit
def test_usage_error_suggests_close_command(capsys: pytest.CaptureFixture[str]) -> None:
    _, res, _ = run(capsys, "cutt", "x.mp4")
    assert "'cut'" in res["error"]["message"]


@pytest.mark.unit
def test_usage_error_human_goes_to_stderr(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    force_tty(monkeypatch)
    assert cli.main(["cut"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "✗ 参数不对" in err and "→" in err


@pytest.mark.unit
def test_usage_error_with_json_flag_even_on_tty(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    force_tty(monkeypatch)
    assert cli.main(["cut", "--json"]) == 2
    out = capsys.readouterr().out
    assert json.loads(out)["error"]["code"] == "usage"


@pytest.mark.unit
def test_non_tty_defaults_to_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)
    code, res, _ = run(capsys, "show", str(p))  # 没加 --json；capsys 的 stdout 不是终端
    assert code == 0 and res["ok"] and res["revision"] == 1


@pytest.mark.unit
def test_tty_prints_human_summary(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = synthetic_plan(tmp_path)
    force_tty(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli.main(["show", str(p)]) == 0
    out = capsys.readouterr().out
    assert "✓ 方案" in out and "✂" in out and "⚠ 拿不准 12" in out and "下一步" in out
    assert "\x1b[" not in out  # NO_COLOR
    assert not out.lstrip().startswith("{")


@pytest.mark.unit
def test_tty_colors_unless_no_color(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = synthetic_plan(tmp_path)
    force_tty(monkeypatch)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert cli.main(["show", str(p), "--filter", "uncertain"]) == 0
    assert "\x1b[" in capsys.readouterr().out


@pytest.mark.unit
def test_every_command_has_human_renderer() -> None:
    assert set(cli.HUMAN) == set(cli.COMMANDS)


@pytest.mark.unit
def test_missing_plan_and_invalid_plan_exit_3(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, res, _ = run(capsys, "show", str(tmp_path / "nope.autocut.json"))
    assert code == 3 and res["error"]["code"] == "plan_not_found"
    bad = tmp_path / "bad.autocut.json"
    bad.write_text('{"autocut_plan": 1, "outputs": [{"id": "main", "segments": [{"id": "a", "source": "x", "start": 2, "end": 1}]}]}')
    code, res, _ = run(capsys, "show", str(bad))
    assert code == 3 and res["error"]["code"] == "plan_invalid"
    assert "outputs[0].segments[0]" in res["error"]["message"]  # 带上 Plan.load 的具体原因
    code, res, _ = run(capsys, "show", str(MEDIA / "talk_zh.mp4"))
    assert code == 3 and res["error"]["code"] == "not_a_plan" and "autocut cut" in res["error"]["fix"]


@pytest.mark.unit
def test_missing_ffmpeg_exit_4(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    real = cli.shutil.which
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: None if name in ("ffmpeg", "ffprobe") else real(name, *a, **k))
    code, res, _ = run(capsys, "cut", "talk.mp4")
    assert code == 4 and res["error"]["code"] == "missing_ffmpeg" and "brew install ffmpeg" in res["error"]["fix"]


@pytest.mark.unit
def test_unexpected_error_exit_1_traceback_only_with_debug(capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    p = synthetic_plan(tmp_path)

    def boom(args: Any, ui: Any) -> dict[str, Any]:
        raise RuntimeError("出事了")

    monkeypatch.setitem(cli.COMMANDS, "show", boom)
    code, res, err = run(capsys, "show", str(p))
    assert code == 1 and res["error"]["code"] == "internal" and "出事了" in res["error"]["message"]
    assert "Traceback" not in err and "--debug" in res["error"]["fix"]
    code, res, err = run(capsys, "show", str(p), "--debug")
    assert code == 1 and "Traceback" in err and "RuntimeError" in err


@pytest.mark.unit
def test_global_flags_before_or_after_command(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = str(synthetic_plan(tmp_path))
    force_tty(monkeypatch)  # 终端里也能用 --json 强制 JSON
    for argv in (["--json", "show", p], ["show", p, "--json"]):
        assert cli.main(argv) == 0
        assert json.loads(capsys.readouterr().out)["command"] == "show"
    monkeypatch.undo()
    for argv in (["-v", "show", p], ["show", p, "-v"]):
        assert cli.main(argv) == 0
        err = capsys.readouterr().err
        assert "INFO autocut.cli: show 完成" in err  # 日志只进 stderr，非终端时是纯文本
    assert cli.main(["show", p]) == 0
    assert "INFO" not in capsys.readouterr().err  # 默认只写警告


@pytest.mark.unit
def test_broken_pipe_is_quiet(tmp_path: Path) -> None:
    """`autocut show ... | head -c 10`：下游提前关管道时不打 traceback。"""
    p = synthetic_plan(tmp_path, n=600)
    r = subprocess.run(f"{sys.executable} -m autocut.cli show {p} --segments --limit 500 | head -c 10", shell=True,
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.stdout.startswith('{"ok":true') and "Traceback" not in r.stderr and "BrokenPipe" not in r.stderr


@pytest.mark.unit
def test_logging_state_restored_after_main(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    import logging

    lg = logging.getLogger("autocut")
    before = (lg.level, list(lg.handlers))
    run(capsys, "show", str(synthetic_plan(tmp_path)), "-vv")
    assert (lg.level, list(lg.handlers)) == before


# ================================================================== 单元：show（有界输出）


@pytest.mark.unit
def test_show_summary_bounds_uncertain(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, res, _ = run(capsys, "show", str(synthetic_plan(tmp_path)))
    s = res["summary"]
    assert code == 0 and res["output"] == "main"
    assert s["uncertain_total"] == 12 and len(s["uncertain"]) == cli.UNCERTAIN_LIMIT
    u = s["uncertain"][0]
    assert set(u) >= {"id", "at", "out_at", "action", "reason", "confidence", "detail", "text"}
    assert s["cut"] == {"silence": 40} and s["degraded"] is True  # 素材没有文字稿，又要求了重说/口头禅
    assert any("--filter uncertain" in n["cmd"] for n in res["next"])


@pytest.mark.unit
def test_show_segments_pagination(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = str(synthetic_plan(tmp_path))
    code, res, _ = run(capsys, "show", p, "--segments")
    assert code == 0
    assert (res["total"], res["showing"], res["next_offset"]) == (120, 50, 50)
    assert "只显示了 50/120 条" in res["notice"] and "--offset 50" in res["notice"] and "--filter uncertain" in res["notice"]
    assert [s["id"] for s in res["segments"][:2]] == ["d001", "d002"]
    assert any(n["cmd"].endswith("--offset 50") for n in res["next"])
    row = res["segments"][1]
    assert set(row) >= {"id", "start", "end", "out_at", "action", "reason", "confidence", "detail", "text", "edited"}
    assert res["segments"][3]["out_at"] == 2.0  # 前面剪掉 1 个、保留 2 个 1 秒的片段

    _, last, _ = run(capsys, "show", p, "--segments", "--offset", "100")
    assert (last["showing"], last["next_offset"]) == (20, None) and "notice" not in last
    _, small, _ = run(capsys, "show", p, "--segments", "--limit", "5", "--offset", "5")
    assert [s["id"] for s in small["segments"]] == ["d006", "d007", "d008", "d009", "d010"] and small["next_offset"] == 10
    code, huge, _ = run(capsys, "show", p, "--segments", "--limit", "5000")
    assert code == 2 and huge["error"]["code"] == "bad_option" and "--offset" in huge["error"]["fix"]


@pytest.mark.unit
def test_show_filters_imply_segments(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = str(synthetic_plan(tmp_path))
    _, unc, _ = run(capsys, "show", p, "--filter", "uncertain")
    assert unc["total"] == 12 and all(s["confidence"] < 0.75 for s in unc["segments"])
    assert any(n["cmd"].startswith("autocut set") for n in unc["next"])  # 给出可以直接改的例子
    _, cut, _ = run(capsys, "show", p, "--filter", "cut")
    assert cut["total"] == 40 and {s["action"] for s in cut["segments"]} == {"cut"}
    _, sil, _ = run(capsys, "show", p, "--filter", "silence")
    assert sil["total"] == 40
    _, src_near, _ = run(capsys, "show", p, "--around", "1:00", "--source-time")
    assert src_near["timebase"] == "source" and {s["id"] for s in src_near["segments"]} == {f"d{i:03d}" for i in range(50, 72)}


@pytest.mark.unit
def test_show_around_defaults_to_output_time(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """用户说"1:00 那段"是看成片说的：默认按成片时间找；剪掉的片段给它在成片里的接缝位置。"""
    p = str(synthetic_plan(tmp_path))
    _, res, _ = run(capsys, "show", p, "--around", "1:00")
    assert res["timebase"] == "output" and res["around"] == 60.0
    # 每 3 秒素材留 2 秒：成片 1:00 附近 = 素材 1:30 附近
    for s in res["segments"]:
        span = (s["out_at"], s["out_at"] + (1.0 if s["action"] == "keep" else 0.0))
        assert span[1] >= 50 and span[0] <= 70, s
        assert {"start", "end", "out_at"} <= set(s)
    ids = [int(s["id"][1:]) for s in res["segments"]]
    assert min(ids) >= 70 and max(ids) <= 110 and 85 <= ids[len(ids) // 2] <= 95
    _, far, _ = run(capsys, "show", p, "--around", "9:00")
    assert far["total"] == 0 and "超出了成片" in far["notice"] and "--source-time" in far["notice"]


@pytest.mark.unit
def test_empty_results_carry_notice(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    plan = Plan(recipe="cut", sources={"a": Source(id="a", path=str(MEDIA / "talk_zh.mp4"), duration=10.0)},
                outputs=[Output("main", segments=[Segment("d001", "a", 0.0, 5.0), Segment("d002", "a", 5.0, 9.0, action="cut", reason="silence")])],
                options={"retakes": False, "fillers": False})
    p = str(plan.save(tmp_path / "sure.autocut.json"))
    _, unc, _ = run(capsys, "show", p, "--filter", "uncertain")
    assert unc["segments"] == [] and unc["notice"].startswith("0 处拿不准：全部有把握")
    _, summ, _ = run(capsys, "show", p)
    assert summ["summary"]["uncertain"] == [] and summ["notice"].startswith("0 处拿不准")
    _, ret, _ = run(capsys, "show", p, "--filter", "retake")
    assert ret["total"] == 0 and "retake" in ret["notice"]


@pytest.mark.unit
def test_show_unknown_output_exit_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = str(multi_output_plan(tmp_path))
    code, res, _ = run(capsys, "show", p, "--output", "clip_9")
    assert code == 2 and res["error"]["code"] == "unknown_output" and "clip_1" in res["error"]["fix"]


@pytest.mark.unit
def test_show_highlights_plan(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = str(multi_output_plan(tmp_path))
    _, res, _ = run(capsys, "show", p)
    s = res["summary"]
    assert [c["id"] for c in s["candidates"]] == ["clip_1", "clip_2", "clip_3"] and s["selected"] == ["clip_1", "clip_2"]
    _, allo, _ = run(capsys, "show", p, "--output", "all")
    assert allo["total"] == 3 and allo["outputs"][2]["uncertain"] == 1
    _, one, _ = run(capsys, "show", p, "--output", "clip_3")
    assert one["summary"]["uncertain_total"] == 1 and one["summary"]["selected"] is False


@pytest.mark.unit
def test_highlights_summary_shows_shortfall_and_weak(capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """配方宁缺毋滥：只选中 k 条（要求 n 条）时摘要和 notice 要说清楚，弱候选单独标出来，set --select 能选上。"""
    p = multi_output_plan(tmp_path)
    plan = Plan.load(p)
    plan.options["n"] = 3
    plan.outputs[2].reasons = ["响度补位：只有音量比基线高，没有弹幕或关键词佐证，把握度低，没自动选"]
    plan.save()
    _, res, _ = run(capsys, "show", str(p))
    s = res["summary"]
    assert s["requested"] == 3 and s["selected"] == ["clip_1", "clip_2"] and s["weak"] == ["clip_3"]
    assert next(c for c in s["candidates"] if c["id"] == "clip_3")["weak"] is True
    assert "只选中 2 条（要求 3 条）" in res["notice"] and f"{p.name} clip_3 --select" in res["notice"]
    force_tty(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli.main(["show", str(p)]) == 0
    human = capsys.readouterr().out
    assert "△ clip_3" in human and "弱候选" in human and "只选中 2 条（要求 3 条）" in human
    monkeypatch.undo()
    code, sel, _ = run(capsys, "set", str(p), "clip_3", "--select")
    assert code == 0 and sel["selected"] == ["clip_1", "clip_2", "clip_3"]


# ================================================================== 单元：set（幂等、revision、校验）


@pytest.mark.unit
def test_set_changes_and_is_idempotent(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)
    code, res, _ = run(capsys, "set", str(p), "d001", "d004", "--keep")
    assert code == 0 and res["revision"] == 2
    assert res["changed"] == [{"id": "d001", "action": "keep", "was": "cut"}, {"id": "d004", "action": "keep", "was": "cut"}]
    assert res["duration"]["after"] == pytest.approx(res["duration"]["before"] + 2.0)
    seg = Plan.load(p).output().find("d001")[1]
    assert seg.action == "keep" and seg.confidence == 1.0 and seg.evidence["edited"] is True

    mtime = p.stat().st_mtime_ns
    code, again, _ = run(capsys, "set", str(p), "d001", "d004", "--keep")
    assert code == 0 and again["revision"] == 2 and again["changed"] == [] and again["unchanged"] == ["d001", "d004"]
    assert "没有变化" in again["notice"] and again["notes"] == []
    assert p.stat().st_mtime_ns == mtime and Plan.load(p).revision == 2


@pytest.mark.unit
def test_set_keep_on_uncertain_keep_confirms_it(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)  # d006：保留、把握 0.6
    _, res, _ = run(capsys, "set", str(p), "d006", "--keep")
    assert res["changed"] == [{"id": "d006", "action": "keep", "was": "keep", "confirmed": True}] and res["revision"] == 2
    _, again, _ = run(capsys, "set", str(p), "d006", "--keep")
    assert again["changed"] == [] and again["revision"] == 2


@pytest.mark.unit
def test_set_expect_revision_conflict(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)
    before = p.read_bytes()
    code, res, _ = run(capsys, "set", str(p), "d001", "--keep", "--expect-revision", "3")
    assert code == 3 and res["error"]["code"] == "revision_conflict"
    assert "rev 1" in res["error"]["message"] and "rev 3" in res["error"]["message"] and "autocut show" in res["error"]["fix"]
    assert p.read_bytes() == before
    code, ok, _ = run(capsys, "set", str(p), "d001", "--keep", "--expect-revision", "1")
    assert code == 0 and ok["revision"] == 2


@pytest.mark.unit
def test_set_dry_run_does_not_write(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)
    before = p.read_bytes()
    code, res, _ = run(capsys, "set", str(p), "d001", "--keep", "--dry-run")
    assert code == 0 and res["dry_run"] is True and res["changed"][0]["was"] == "cut" and res["revision"] == 1
    assert p.read_bytes() == before
    assert "--dry-run" not in res["next"][0]["cmd"]


@pytest.mark.unit
def test_set_unknown_id_suggests_close_ids(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)
    code, res, _ = run(capsys, "set", str(p), "d0011", "--cut")
    assert code == 3 and res["error"]["code"] == "unknown_id"
    assert "d0011" in res["error"]["message"] and "相近的 id" in res["error"]["fix"] and "d011" in res["error"]["fix"]
    assert "autocut show" in res["error"]["fix"]


@pytest.mark.unit
def test_set_boundary(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)
    code, res, _ = run(capsys, "set", str(p), "d002", "--end", "2.5")
    assert code == 0 and res["changed"] == [{"id": "d002", "edge": "end", "to": 2.5, "was": 2.0, "moved": ["d003"]}]
    plan = Plan.load(p)
    assert plan.output().find("d003")[1].start == 2.5
    code, bad, _ = run(capsys, "set", str(p), "d002", "--end", "9")
    assert code == 2 and bad["error"]["code"] == "boundary_out_of_range"
    assert "1.01–2.99" in bad["error"]["message"] and "--end 2.99" in bad["error"]["fix"]
    code, two, _ = run(capsys, "set", str(p), "d002", "d003", "--end", "2.4")
    assert code == 2 and two["error"]["code"] == "usage"
    code, none, _ = run(capsys, "set", str(p), "d002")
    assert code == 2 and none["error"]["code"] == "usage"


@pytest.mark.unit
def test_set_select_and_ambiguous_ids(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = multi_output_plan(tmp_path)
    code, res, _ = run(capsys, "set", str(p), "clip_3", "--select")
    assert code == 0 and res["changed"] == [{"id": "clip_3", "selected": True, "was": False}]
    assert res["selected"] == ["clip_1", "clip_2", "clip_3"] and res["revision"] == 2
    _, again, _ = run(capsys, "set", str(p), "clip_3", "--select")
    assert again["changed"] == [] and again["revision"] == 2
    code, amb, _ = run(capsys, "set", str(p), "c002", "--keep")
    assert code == 2 and amb["error"]["code"] == "ambiguous_id" and "--output" in amb["error"]["fix"]
    code, ok, _ = run(capsys, "set", str(p), "c002", "--cut", "--output", "clip_3")
    assert code == 0 and ok["changed"][0] == {"id": "c002", "action": "cut", "was": "keep", "output": "clip_3"}
    code, wrong, _ = run(capsys, "set", str(p), "clip_2", "--keep")
    assert code == 2 and "--select" in wrong["error"]["fix"]
    code, miss, _ = run(capsys, "set", str(p), "clip_9", "--deselect")
    assert code == 3 and "clip_" in miss["error"]["fix"]


@pytest.mark.unit
def test_recipe_refuses_to_overwrite_existing_plan(tmp_path: Path) -> None:
    p = synthetic_plan(tmp_path)
    assert cli._prepare_plan_target(tmp_path / "new.autocut.json", force=False, dry_run=False) == cli._PlanTarget()
    plan = Plan.load(p)
    plan.set_action("d001", "keep")
    plan.save()
    with pytest.raises(cli.CliError) as e:
        cli._prepare_plan_target(p, force=False, dry_run=False)
    err = e.value
    assert err.code == "plan_exists" and err.exit_code == 3
    assert "rev 2" in err.message and "1 个片段被人改过" in err.message  # 现有方案的 revision 和人改过几处
    assert "-o talk.v2.autocut.json" in err.fix and "--force" in err.fix
    # --dry-run 不写文件，可以照常预演；--force 接着旧 revision 往上加（审阅页靠 revision 发现外部修改）
    assert cli._prepare_plan_target(p, force=False, dry_run=True) == cli._PlanTarget(base_rev=2, edits=1, exists=True)
    assert cli._prepare_plan_target(p, force=True, dry_run=False) == cli._PlanTarget(base_rev=2, edits=1, exists=True)
    junk = tmp_path / "junk.autocut.json"
    junk.write_text("not json")
    with pytest.raises(cli.CliError) as e:
        cli._prepare_plan_target(junk, force=False, dry_run=False)
    assert e.value.code == "plan_exists" and "读不了" in e.value.message


# ================================================================== 单元：review（不开浏览器）


@pytest.mark.unit
def test_review_calls_serve_without_browser(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autocut.review import server

    p = synthetic_plan(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_serve(plan_path: Any, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
        calls.append({"plan": Path(plan_path), "port": port, "open_browser": open_browser})
        plan = Plan.load(plan_path)  # 模拟人在审阅页里改了一处
        plan.set_action("d001", "keep")
        plan.save()

    monkeypatch.setattr(server, "serve", fake_serve)
    force_tty(monkeypatch)  # 终端里：前台运行
    assert cli.main(["review", str(p), "--no-open", "--port", "8765", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert calls == [{"plan": p, "port": 8765, "open_browser": False}]
    assert res["revision_before"] == 1 and res["revision"] == 2 and res["changed"] is True


@pytest.mark.unit
def test_review_would_block_without_tty(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autocut.review import server

    monkeypatch.setattr(server, "serve", lambda *a, **k: pytest.fail("非终端时不该在前台启动服务"))
    code, res, _ = run(capsys, "review", str(synthetic_plan(tmp_path)))
    assert code == 2 and res["error"]["code"] == "would_block"
    assert "交给用户自己运行" in res["error"]["fix"] and "--detach" in res["error"]["fix"]


@pytest.mark.unit
def test_review_dry_run_does_not_start(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autocut.review import server

    monkeypatch.setattr(server, "serve", lambda *a, **k: pytest.fail("dry-run 不该启动服务"))
    code, res, _ = run(capsys, "review", str(synthetic_plan(tmp_path)), "--dry-run")
    assert code == 0 and res["dry_run"] is True and res["revision"] == 1


# ================================================================== 单元：render / export 不覆盖（只查存在，不跑 ffmpeg）


@pytest.mark.unit
def test_render_refuses_to_overwrite(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    needs_media()
    p = synthetic_plan(tmp_path, n=20)
    out = tmp_path / "talk.main.mp4"
    out.write_bytes(b"old")
    code, res, _ = run(capsys, "render", str(p))
    assert code == 3 and res["error"]["code"] == "exists" and "--force" in res["error"]["fix"]
    assert out.read_bytes() == b"old"


@pytest.mark.unit
def test_export_refuses_to_overwrite(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    needs_media()
    p = synthetic_plan(tmp_path, n=20)
    out = tmp_path / "talk.main.fcpxml"
    out.write_text("old")
    code, res, _ = run(capsys, "export", str(p), "--format", "fcpxml")
    assert code == 3 and res["error"]["code"] == "exists"
    assert out.read_text() == "old"


@pytest.mark.unit
def test_render_nothing_selected_and_missing_source(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    p = multi_output_plan(tmp_path)
    plan = Plan.load(p)
    for o in plan.outputs:
        o.selected = False
    plan.save()
    code, res, _ = run(capsys, "render", str(p))
    assert code == 3 and res["error"]["code"] == "nothing_selected" and "--select" in res["error"]["fix"]
    plan.sources["vod"].path = str(tmp_path / "gone.mp4")
    plan.save()
    code, res, _ = run(capsys, "render", str(p), "--output", "clip_1")
    assert code == 3 and res["error"]["code"] == "source_missing"


# ================================================================== 集成：在 fixtures 上跑真实配方


@pytest.fixture(scope="module")
def cut_plan(tmp_path_factory: pytest.TempPathFactory, asr_cache: Path) -> tuple[Path, dict[str, Any]]:
    """cut talk_zh.mp4 一次，供后面 show / render / export / check 共用（写在临时目录，不碰 fixtures）。"""
    needs_media()
    d = tmp_path_factory.mktemp("cli-cut")
    p = d / "talk.autocut.json"
    code = cli.main(["cut", str(MEDIA / "talk_zh.mp4"), "-o", str(p), "--json"])
    assert code == 0 and p.exists()
    return p, json.loads(p.read_text())


def _size(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], int]:
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out), len(out.encode())


@pytest.mark.integration
def test_cut_contract(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    p = tmp_path / "talk.autocut.json"
    code, res, _ = run(capsys, "cut", str(MEDIA / "talk_zh.mp4"), "-o", str(p))
    assert code == 0 and p.exists() and res["revision"] == 1
    s = res["summary"]
    assert set(s) == {"duration", "source_span", "removed", "cut", "uncertain", "uncertain_total", "degraded"}
    assert s["degraded"] is False and s["cut"].get("silence", 0) >= 3 and s["cut"].get("retake", 0) >= 1
    assert s["duration"] + s["removed"] == pytest.approx(s["source_span"], abs=0.05)
    assert res["analysis"]["asr"] and res["analysis"]["track"].startswith("音轨 1")
    cmds = [n["cmd"] for n in res["next"]]
    assert any(c.startswith("autocut review") for c in cmds) and any(c.startswith("autocut render") for c in cmds)
    assert Plan.load(p).revision == 1
    # 重跑：已有方案不覆盖（保护人在审阅页里的修改）；--force 才替换，revision 接着往上加（审阅页靠它发现外部修改）
    before = p.read_bytes()
    code, refused, _ = run(capsys, "cut", str(MEDIA / "talk_zh.mp4"), "-o", str(p), "--pace", "relaxed")
    assert code == 3 and refused["error"]["code"] == "plan_exists" and p.read_bytes() == before
    code, again, _ = run(capsys, "cut", str(MEDIA / "talk_zh.mp4"), "-o", str(p), "--pace", "relaxed", "--force")
    assert code == 0 and again["revision"] == 2 and again["summary"]["duration"] > s["duration"]


@pytest.mark.integration
def test_cut_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    p = tmp_path / "talk.autocut.json"
    code, res, _ = run(capsys, "cut", str(MEDIA / "talk_zh.mp4"), "-o", str(p), "--dry-run")
    assert code == 0 and res["dry_run"] is True and res["revision"] == 0
    assert not p.exists() and list(tmp_path.iterdir()) == []
    assert res["next"][0]["cmd"].startswith("autocut cut") and "--dry-run" not in res["next"][0]["cmd"]


@pytest.mark.integration
def test_cut_without_asr_is_degraded_not_silent(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    needs_media()
    monkeypatch.setenv("AUTOCUT_WHISPER_CLI", str(tmp_path / "no-such-whisper-cli"))
    code, res, _ = run(capsys, "cut", str(MEDIA / "talk_zh.mp4"), "-o", str(tmp_path / "t.autocut.json"))
    assert code == 0
    s = res["summary"]
    assert s["degraded"] is True and set(s["cut"]) <= {"silence"} and s["cut"]["silence"] >= 3
    assert res["analysis"]["asr"] is None
    notes = " ".join(res["notes"])
    assert "whisper-cli" in notes and "重说和口头禅未处理" in notes  # 配方的 notes 原样转出
    assert "没检测重说/口头禅" in notes and "autocut doctor" in notes  # 补法
    assert res["next"][0]["cmd"] == "autocut doctor"


@pytest.mark.integration
def test_cut_input_errors(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    code, res, _ = run(capsys, "cut", str(tmp_path / "nope.mp4"))
    assert code == 3 and res["error"]["code"] == "not_found" and "当前目录" in res["error"]["message"]
    code, res, _ = run(capsys, "cut", str(MEDIA / "game_commentary.mp4"), "--track", "5", "--dry-run")
    assert code == 2 and res["error"]["code"] == "track_out_of_range"
    assert "只有 3 条音轨：1 mix、2 mic、3 game" in res["error"]["message"] and res["error"]["fix"] == "用 --track 2"
    silent = tmp_path / "noaudio.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1", str(silent)], check=True)
    code, res, _ = run(capsys, "cut", str(silent))
    assert code == 3 and res["error"]["code"] == "no_audio" and "montage" in res["error"]["fix"]
    code, res, _ = run(capsys, "cut", str(MEDIA / "talk_zh.mp4"), "-o", str(tmp_path / "x.txt"))
    assert code == 2 and ".json" in res["error"]["message"]


@pytest.mark.integration
def test_cut_track_by_title(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    code, res, _ = run(capsys, "cut", str(MEDIA / "game_commentary.mp4"), "--track", "Mic", "-o", str(tmp_path / "g.autocut.json"))
    assert code == 0 and res["analysis"]["track"] == "音轨 2（mic）"
    assert res["media"][0]["tracks"] == ["mix", "mic", "game"]


@pytest.mark.integration
def test_highlights_contract(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    p = tmp_path / "vod.autocut.json"
    code, res, _ = run(capsys, "highlights", str(MEDIA / "stream_vod.mp4"), "--chat", str(MEDIA / "stream_vod.chat.jsonl"), "-o", str(p))
    assert code == 0 and p.exists()
    s = res["summary"]
    assert len(s["selected"]) == 3 and s["requested"] == 3 and s["baits"] >= 1
    bait = next(c for c in s["candidates"] if c.get("bait"))
    assert bait["selected"] is False and any("刷屏" in r for r in bait["reasons"])
    assert any("授权" in n for n in res["notes"])  # 配方的 notes 原样转出
    assert len(s["candidates"]) <= cli.CANDIDATE_LIMIT


@pytest.mark.integration
def test_highlights_hints_sibling_chat(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    code, res, _ = run(capsys, "highlights", str(MEDIA / "stream_vod.mp4"), "-o", str(tmp_path / "v.autocut.json"), "--dry-run")
    assert code == 0 and any("stream_vod.chat.jsonl" in n for n in res["notes"])
    assert not (tmp_path / "v.autocut.json").exists()


@pytest.mark.integration
def test_montage_contract(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    clips = sorted(str(p) for p in (MEDIA / "montage").glob("clip*.mp4"))
    code, res, _ = run(capsys, "montage", *clips, "--music", str(MEDIA / "montage" / "music_120bpm.wav"), "-o", str(tmp_path / "m.autocut.json"))
    s = res["summary"]
    assert code == 0 and s["bpm"] == pytest.approx(120, abs=1) and s["shots_total"] == len(s["shots"]) > 4
    assert sum(s["sources_used"].values()) == s["shots_total"] and s["sources_total"] == len(clips)
    code, res, _ = run(capsys, "montage", *clips, "--music", str(tmp_path / "missing.wav"))
    assert code == 3 and "BGM" in res["error"]["message"]


@pytest.mark.integration
def test_ytpmv_contract(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    code, res, _ = run(capsys, "ytpmv", str(MEDIA / "ytpmv" / "source_clip.mp4"), "--music", str(MEDIA / "ytpmv" / "music_120bpm.wav"),
                       "-o", str(tmp_path / "y.autocut.json"))
    s = res["summary"]
    assert code == 0 and s["syllables"] >= 8 and s["placements"] >= s["distinct"] > 0 and s["syllable_text"]
    assert any("--format fcpxml" in n["cmd"] for n in res["next"])
    assert any("第一版对拍" in n for n in res["notes"])


@pytest.mark.integration
def test_narrate_contract(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    if "Tingting" not in [n for n, _ in cli.say_voices()]:
        pytest.skip("没有 say 的 Tingting 声音")
    k = MEDIA / "knowledge"
    code, res, _ = run(capsys, "narrate", str(k / "script.md"), *(str(k / f"img{i}.png") for i in (1, 2, 3)),
                       "-o", str(tmp_path / "n.autocut.json"))
    s = res["summary"]
    assert code == 0 and len(s["images"]) == 3 and s["voice"] == "Tingting" and s["hook"]["text"]
    assert sum(i["hold"] for i in s["images"]) == pytest.approx(s["duration"], abs=0.05)
    assert any("核对" in n["why"] for n in res["next"])
    code, res, _ = run(capsys, "narrate", str(k / "script.md"), str(k / "img1.png"), "--voice", "NoSuchVoice", "--dry-run")
    assert code == 4 and res["error"]["code"] == "missing_voice" and "Tingting" in res["error"]["fix"]


@pytest.mark.integration
def test_analyze_suggests_recipes(capsys: pytest.CaptureFixture[str], asr_cache: Path) -> None:
    needs_media()
    code, res, _ = run(capsys, "analyze", str(MEDIA / "talk_zh.mp4"))
    assert code == 0 and res["speech_ratio"] > 0.5 and res["bpm"] is None
    assert res["suggest"][0]["cmd"].startswith("autocut cut") and "说话" in res["suggest"][0]["why"]
    _, vod, _ = run(capsys, "analyze", str(MEDIA / "stream_vod.mp4"))
    assert vod["suggest"][0]["cmd"].startswith("autocut highlights") and "--chat" in vod["suggest"][0]["cmd"]
    _, game, _ = run(capsys, "analyze", str(MEDIA / "game_commentary.mp4"))
    assert [a.get("voice_guess", False) for a in game["audio"]] == [False, True, False]
    assert any(s["cmd"].startswith("autocut cut") and "OBS" in s["why"] for s in game["suggest"])
    _, music, _ = run(capsys, "analyze", str(MEDIA / "montage" / "music_120bpm.wav"))
    assert music["bpm"] == pytest.approx(120, abs=1) and "--music" in music["suggest"][0]["cmd"]
    _, script, _ = run(capsys, "analyze", str(MEDIA / "knowledge" / "script.md"))
    assert script["kind"] == "script" and script["suggest"][0]["cmd"].startswith("autocut narrate")


@pytest.mark.integration
def test_doctor_reports_without_downloading(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    code, res, _ = run(capsys, "doctor")
    assert code == 0 and set(res) >= {"ffmpeg", "asr", "tts", "missing", "fix"}
    assert res["ffmpeg"]["version"] and res["ffmpeg"]["filters"]
    # 没有模型时：只给命令和下载量，不下载
    monkeypatch.setenv("AUTOCUT_WHISPER_CLI", str(tmp_path / "nope"))
    monkeypatch.setattr("autocut.transcribe.find_model", lambda: None)
    code, res, _ = run(capsys, "doctor")
    assert code == 0 and res["asr"]["available"] is False
    assert {"whisper-cli", "语音识别模型"} <= set(res["missing"])
    model_fix = next(f for f in res["fix"] if f["for"] == "语音识别模型")
    assert "curl" in model_fix["cmd"] and "GB" in model_fix["download"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.integration
def test_show_and_set_on_real_plan(capsys: pytest.CaptureFixture[str], cut_plan: tuple[Path, dict[str, Any]]) -> None:
    p, _ = cut_plan
    _, res, _ = run(capsys, "show", str(p))
    assert res["summary"]["cut"] and res["recipe"] == "cut"
    _, segs, _ = run(capsys, "show", str(p), "--filter", "cut")
    first = segs["segments"][0]
    code, changed, _ = run(capsys, "set", str(p), first["id"], "--keep", "--expect-revision", str(res["revision"]))
    assert code == 0 and changed["revision"] == res["revision"] + 1
    assert changed["duration"]["after"] > changed["duration"]["before"]
    run(capsys, "set", str(p), first["id"], "--cut")  # 撤销：再 set 回去


@pytest.mark.integration
def test_render_dry_run_then_render_then_refuse(capsys: pytest.CaptureFixture[str], cut_plan: tuple[Path, dict[str, Any]]) -> None:
    p, _ = cut_plan
    mp4 = p.parent / "talk.main.mp4"
    code, dry, _ = run(capsys, "render", str(p), "--dry-run")
    assert code == 0 and dry["dry_run"] is True and not mp4.exists()
    r = dry["results"][0]
    assert Path(r["filter_graph"]).exists() and Path(r["command_file"]).read_text().startswith("ffmpeg")
    code, res, _ = run(capsys, "render", str(p))
    assert code == 0 and mp4.exists()
    r = res["results"][0]
    assert r["duration"] == pytest.approx(r["expected"], abs=0.04) and r["av_drift"] < 0.04 and r["srt"]
    v = r["verify"]
    assert v["verified"] is True and v["joints"] >= 1 and v["joints_mid_speech"] == 0 and r["warnings"] == []
    assert v["duration"] == pytest.approx(r["expected"], abs=0.04)
    assert res["next"][0]["cmd"].startswith("autocut check")
    mtime = mp4.stat().st_mtime_ns
    code, err, _ = run(capsys, "render", str(p))
    assert code == 3 and err["error"]["code"] == "exists" and mp4.stat().st_mtime_ns == mtime
    code, forced, _ = run(capsys, "render", str(p), "--force", "--no-burn-captions", "--loudnorm", "off", "--no-verify")
    assert code == 0 and mp4.stat().st_mtime_ns != mtime and forced["results"][0]["loudness"] is None
    assert "verify" not in forced["results"][0] and any("--no-verify" in n for n in forced["notes"])
    code, chk, _ = run(capsys, "check", str(mp4), "--no-asr")
    assert code == 0 and chk["mode"] == "file" and "不是爆款保证" in chk["summary"]


@pytest.mark.integration
def test_export_contract(capsys: pytest.CaptureFixture[str], cut_plan: tuple[Path, dict[str, Any]]) -> None:
    p, _ = cut_plan
    out = p.parent / "talk.main.fcpxml"
    code, dry, _ = run(capsys, "export", str(p), "--format", "fcpxml", "--dry-run")
    assert code == 0 and dry["dry_run"] is True and not out.exists()
    code, res, _ = run(capsys, "export", str(p), "--format", "fcpxml")
    assert code == 0 and out.exists() and res["format"] == "fcpxml" and isinstance(res["limits"], list)
    assert res["verified_in_nle"] is False and "还没在剪辑软件里打开过" in res["tell_user"] and "结构已自检" in res["tell_user"]
    assert "say" not in res and any("时间线" in c for c in res["selfcheck"])
    assert any("srt" in x for x in res["limits"]) is False  # cut 的字幕不烧录，没有可提醒的
    code, again, _ = run(capsys, "export", str(p), "--format", "fcpxml")
    assert code == 3 and again["error"]["code"] == "exists"
    for fmt, name in (("edl", "x.edl"), ("xmeml", "x.xml"), ("srt", "x.srt"), ("markers", "x.markers.fcpxml")):
        code, other, _ = run(capsys, "export", str(p), "--format", fmt, "-o", str(p.parent / name))
        assert code == 0 and (p.parent / name).exists() and other["selfcheck"] and other["verified_in_nle"] is False, (fmt, other)


@pytest.mark.integration
def test_check_plan_contract(capsys: pytest.CaptureFixture[str], cut_plan: tuple[Path, dict[str, Any]]) -> None:
    p, _ = cut_plan
    save = p.parent / "report.json"
    code, res, _ = run(capsys, "check", str(p), "--no-asr", "--platform", "douyin", "--save", str(save))
    assert code == 0 and res["mode"] == "plan" and res["platform"] == "douyin" and res["target"].endswith("#main")
    assert res["items_total"] >= 1 and len(res["items"]) <= cli.CHECK_ITEMS_LIMIT
    assert [i["severity"] for i in res["items"]] == sorted((i["severity"] for i in res["items"]), key=cli.SEVERITY_ORDER.get)
    assert "不是爆款保证" in res["summary"] and save.exists() and "dead_air" not in res["metrics"]
    code, res, _ = run(capsys, "check", str(p.parent / "nope.mp4"))
    assert code == 3 and res["error"]["code"] == "not_found"


@pytest.mark.integration
def test_json_stays_within_budget(capsys: pytest.CaptureFixture[str], cut_plan: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    """每个命令的 JSON 默认不超过约 4k token。"""
    p, _ = cut_plan
    big = synthetic_plan(tmp_path, n=600)
    for argv in (["show", str(p)], ["show", str(p), "--segments"], ["show", str(big), "--segments"], ["show", str(big)],
                 ["show", str(big), "--filter", "uncertain"], ["check", str(p), "--no-asr"], ["doctor"]):
        code, _, n = _size(capsys, *argv)
        assert code == 0 and n < JSON_BUDGET, (argv, n)


@pytest.mark.integration
def test_transcribe_contract(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    needs_media()
    from autocut import transcribe

    if not transcribe.available():
        pytest.skip(transcribe.unavailable_reason() or "")
    out = tmp_path / "talk.words.json"
    code, res, err = run(capsys, "transcribe", str(MEDIA / "talk_zh.mp4"), "-o", str(out))
    assert code == 0 and out.exists() and res["format"] == "json" and res["model"] == transcribe.model_tag()
    words = json.loads(out.read_text())
    assert isinstance(words, list) and {"text", "start", "end"} <= set(words[0])
    assert res["words"] > 50 and 0 < len(res["preview"]) <= cli.PREVIEW_LIMIT and res["preview_total"] >= len(res["preview"])
    assert "大家好" in res["preview"][0]["text"] and res["preview"][0]["at"] < 1.0
    assert f"pid={os.getpid()}" in err  # 长命令先报 pid（进程内调用时就是测试进程自己）
    srt = tmp_path / "talk.srt"
    code, s, _ = run(capsys, "transcribe", str(MEDIA / "talk_zh.mp4"), "--format", "srt", "-o", str(srt))
    assert code == 0 and srt.read_text().startswith("1\n00:00:")
    code, t, _ = run(capsys, "transcribe", str(MEDIA / "talk_zh.mp4"), "--format", "txt", "-o", str(tmp_path / "t.txt"))
    assert code == 0 and (tmp_path / "t.txt").read_text().count("\n") == t["preview_total"]
    code, again, _ = run(capsys, "transcribe", str(MEDIA / "talk_zh.mp4"), "-o", str(out))
    assert code == 3 and again["error"]["code"] == "exists"
    code, dry, _ = run(capsys, "transcribe", str(MEDIA / "talk_zh.mp4"), "-o", str(tmp_path / "dry.json"), "--dry-run")
    assert code == 0 and dry["dry_run"] is True and not (tmp_path / "dry.json").exists()


@pytest.mark.integration
def test_transcribe_without_asr_exit_4(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       asr_cache: Path) -> None:
    needs_media()
    monkeypatch.setenv("AUTOCUT_WHISPER_CLI", str(tmp_path / "no-such-whisper-cli"))
    code, res, _ = run(capsys, "transcribe", str(MEDIA / "talk_zh.mp4"), "-o", str(tmp_path / "x.json"))
    assert code == 4 and res["error"]["code"] == "missing_asr"
    assert "whisper-cli" in res["error"]["fix"] and "不会自动下载" in res["error"]["fix"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.integration
def test_render_verify_flags_joint_mid_speech(capsys: pytest.CaptureFixture[str], tmp_path: Path, asr_cache: Path) -> None:
    """接缝切在一句话中间（0:01.5 → 0:05.0，两边都在说话）：成片照样写出，但核验不通过，并说出是哪里。"""
    needs_media()
    t = json.loads((MEDIA / "talk_zh.truth.json").read_text())
    s0, s1 = t["segments"][0], t["segments"][2]  # 两句话：[0, 2.81] 和 [3.16, 9.25]
    assert s0["kind"] == s1["kind"] == "speech"
    segs = [Segment("a1", "a", 0.0, 1.5), Segment("a2", "a", 1.5, 5.0, action="cut", reason="manual"), Segment("a3", "a", 5.0, 9.0)]
    plan = Plan(recipe="manual", sources={"a": Source(id="a", path=str(MEDIA / "talk_zh.mp4"), duration=t["duration"])},
                outputs=[Output("main", segments=segs)])
    p = plan.save(tmp_path / "mid.autocut.json")
    code, res, _ = run(capsys, "render", str(p))
    r = res["results"][0]
    assert code == 0 and res["ok"] is True and (tmp_path / "mid.main.mp4").exists()
    v = r["verify"]
    assert v["verified"] is False and v["joints"] == 1 and v["joints_mid_speech"] == 1
    assert v["mid_speech_at"] == [1.5] and any("说话中间" in w for w in r["warnings"])
    assert "没通过核验" in res["notice"] and res["next"][0]["cmd"].endswith("--around 00:01.5")


@pytest.mark.integration
def test_review_detach_starts_background_server(tmp_path: Path) -> None:
    if not AUTOCUT_BIN.exists():
        pytest.skip("没有 .venv/bin/autocut")
    import signal
    import time
    import urllib.request

    p = synthetic_plan(tmp_path)
    r = subprocess.run([str(AUTOCUT_BIN), "review", str(p), "--detach", "--no-open"], capture_output=True, text=True, timeout=60)
    res = json.loads(r.stdout)
    assert r.returncode == 0 and res["ok"] and res["url"].startswith("http://127.0.0.1:") and res["stop"] == f"kill {res['pid']}"
    pid = res["pid"]
    try:
        assert os.getpgid(pid) == pid  # 独立进程组：调用方退出、被 Ctrl+C 都不影响它
        with urllib.request.urlopen(res["url"] + "api/plan/status", timeout=10) as resp:
            assert resp.status == 200 and json.loads(resp.read())["revision"] == 1
    finally:
        os.kill(pid, signal.SIGTERM)  # 只按它报告的 pid 杀
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"审阅页进程 {pid} 收到 SIGTERM 后没有退出")


@pytest.mark.integration
def test_render_cancel_cleans_up(tmp_path: Path) -> None:
    """kill <pid>（SIGTERM）：停掉 ffmpeg 子进程、删掉写了一半的文件、退出码 5（cancelled）；之后重跑不会被"不覆盖"挡住。"""
    needs_media()
    if not AUTOCUT_BIN.exists():
        pytest.skip("没有 .venv/bin/autocut")
    import signal
    import time

    segs = [Segment(f"s{i}", "v", 0.0 if i % 2 == 0 else 1.0, 149.0) for i in range(6)]  # 约 15 分钟的成片，足够在编码中途取消
    plan = Plan(recipe="manual", sources={"v": Source(id="v", path=str(MEDIA / "stream_vod.mp4"), duration=150.0)},
                outputs=[Output("main", segments=segs)])
    p = plan.save(tmp_path / "long.autocut.json")
    env = {**os.environ, "AUTOCUT_CACHE": str(tmp_path / "cache")}
    proc = subprocess.Popen([str(AUTOCUT_BIN), "render", str(p), "--loudnorm", "off"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    try:
        first = proc.stderr.readline()
        assert first.startswith(f"pid={proc.pid}") and f"kill {proc.pid}" in first
        part = tmp_path / ".long.main.part.mp4"  # render 写成片时用的临时文件名（写完才 rename 成 long.main.mp4）
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not part.exists():
            assert proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        assert part.exists(), "ffmpeg 一直没开始写成片"
        ps = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
        kids = [int(ln.split()[0]) for ln in ps.splitlines() if ln.split()[1:2] == [str(proc.pid)]]
        assert kids, "没找到 render 启动的 ffmpeg"
        os.kill(proc.pid, signal.SIGTERM)  # 只按 render 报告的 pid 发
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 5, (out, err)
    res = json.loads(out)
    assert res["error"]["code"] == "cancelled" and res["error"]["retry"] is True and "Traceback" not in err
    for left in (part, tmp_path / "long.main.mp4", tmp_path / "long.main.srt"):
        assert not left.exists(), f"取消后还留着 {left.name}"
    assert sorted(x.name for x in tmp_path.iterdir() if x.name != "cache") == ["long.autocut.json"]
    time.sleep(0.2)
    for k in kids:
        with pytest.raises(ProcessLookupError):
            os.kill(k, 0)


# ================================================================== 真正的 autocut 入口（子进程）


@pytest.mark.integration
def test_entry_point_subprocess(tmp_path: Path) -> None:
    if not AUTOCUT_BIN.exists():
        pytest.skip("没有 .venv/bin/autocut：先 uv pip install --offline -e .")
    env = {**os.environ, "AUTOCUT_CACHE": str(tmp_path / "cache")}
    r = subprocess.run([str(AUTOCUT_BIN), "--help"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "全局约定" in r.stdout
    r = subprocess.run([str(AUTOCUT_BIN), "doctor"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0
    assert json.loads(r.stdout)["command"] == "doctor"  # 管道不是终端 → 自动 JSON，stdout 只有这一个对象
    r = subprocess.run([str(AUTOCUT_BIN), "cut", str(tmp_path / "missing.mp4")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 3 and json.loads(r.stdout)["error"]["code"] == "not_found" and "Traceback" not in r.stderr
    p = synthetic_plan(tmp_path)
    r = subprocess.run([str(AUTOCUT_BIN), "show", str(p), "--filter", "uncertain", "--limit", "3"], capture_output=True, text=True,
                       env=env, timeout=60)
    res = json.loads(r.stdout)
    assert r.returncode == 0 and (res["total"], res["showing"], res["next_offset"]) == (12, 3, 3)
    r = subprocess.run([str(AUTOCUT_BIN), "set", str(p), "d001", "--keep", "--expect-revision", "9"], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 3 and json.loads(r.stdout)["error"]["code"] == "revision_conflict"
