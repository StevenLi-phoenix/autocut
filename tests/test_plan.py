"""方案数据模型的不变量：序列化往返、校验消息、时间映射、审阅修改。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocut.plan import Captions, Output, Plan, PlanError, Segment, Source

pytestmark = pytest.mark.unit


def make_plan(tmp_path: Path | None = None) -> Plan:
    segs = [
        Segment("d001", "main", 0.0, 3.0, "keep", "speech", text="大家好"),
        Segment("d002", "main", 3.0, 5.0, "cut", "silence", 0.96, detail="停顿 2.0s"),
        Segment("d003", "main", 5.0, 6.0, "cut", "filler", 0.6, text="嗯"),
        Segment("d004", "main", 6.0, 9.5, "keep", "speech", text="今天聊剪辑"),
    ]
    plan = Plan(
        recipe="cut",
        sources={"main": Source("main", "talk.mp4", duration=10.0)},
        outputs=[Output("main", segs, captions=Captions(enabled=True))],
    )
    if tmp_path is not None:
        plan.path = tmp_path / "talk.autocut.json"
    return plan


def test_duration_and_timeline() -> None:
    o = make_plan().output()
    assert o.duration == pytest.approx(6.5)
    tl = o.timeline()
    assert [(s.id, a, b) for s, a, b in tl] == [("d001", 0.0, 3.0), ("d004", 3.0, 6.5)]


def test_to_output_time_maps_kept_and_rejects_cut() -> None:
    o = make_plan().output()
    assert o.to_output_time("main", 1.5) == pytest.approx(1.5)
    assert o.to_output_time("main", 7.0) == pytest.approx(4.0)
    assert o.to_output_time("main", 4.0) is None


def test_slot_and_speed_change_out_duration() -> None:
    s = Segment("x", "main", 0, 2, speed=2.0)
    assert s.out_duration == pytest.approx(1.0)
    s.slot = 0.5
    assert s.out_duration == pytest.approx(0.5)


def test_summary_counts_and_uncertain() -> None:
    summ = make_plan().summary()
    o = summ["outputs"][0]
    assert o["cut"] == {"silence": 1, "filler": 1}
    assert o["uncertain"] == ["d003"]
    assert o["removed"] == pytest.approx(3.0)
    assert o["duration"] == pytest.approx(6.5)


def test_roundtrip_save_load(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    p = plan.save()
    assert plan.revision == 1
    raw = json.loads(p.read_text())
    assert raw["autocut_plan"] == 1
    assert "path" not in raw
    # 默认空字段被省略，文件更短
    assert "evidence" not in raw["outputs"][0]["segments"][0]
    again = Plan.load(p)
    assert again.output().duration == pytest.approx(6.5)
    assert again.revision == 1
    assert again.source_path("main") == tmp_path / "talk.mp4"


def test_set_action_marks_confirmed() -> None:
    plan = make_plan()
    seg = plan.set_action("d003", "keep")
    assert seg.action == "keep" and seg.confidence == 1.0 and seg.evidence["edited"]
    assert seg.reason == "filler"  # 保留机器当初的理由
    assert plan.output().duration == pytest.approx(7.5)
    with pytest.raises(PlanError, match="action 只能是"):
        plan.set_action("d003", "delete")
    with pytest.raises(PlanError, match="没有 id"):
        plan.set_action("nope", "keep")


def test_move_boundary_moves_joined_neighbor() -> None:
    plan = make_plan()
    plan.move_boundary("d001", "end", 3.4)
    o = plan.output()
    assert o.segments[0].end == pytest.approx(3.4)
    assert o.segments[1].start == pytest.approx(3.4)
    with pytest.raises(PlanError, match="超出允许范围"):
        plan.move_boundary("d001", "end", 5.5)


def test_validate_reports_paths_and_valid_values() -> None:
    plan = make_plan()
    plan.output().segments[1].reason = "boring"
    plan.output().segments[2].end = 4.0  # end < start
    plan.output().segments[3].source = "cam2"
    with pytest.raises(PlanError) as e:
        plan.validate()
    msg = str(e.value)
    assert "3 处问题" in msg
    assert "segments[1]" in msg and "reason='boring'" in msg and "silence" in msg
    assert "end (4.0) 必须大于 start (5.0)" in msg
    assert "source='cam2'" in msg and "可用" in msg


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    d = make_plan().to_dict()
    d["outputs"][0]["segments"][0]["actoin"] = "cut"
    with pytest.raises(PlanError, match="不认识字段.*actoin"):
        Plan.from_dict(d)


def test_load_errors_are_actionable(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="找不到方案文件"):
        Plan.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    with pytest.raises(PlanError, match="不是合法 JSON"):
        Plan.load(bad)
    notplan = tmp_path / "other.json"
    notplan.write_text("{}")
    with pytest.raises(PlanError, match="不是 autocut 方案"):
        Plan.load(notplan)
