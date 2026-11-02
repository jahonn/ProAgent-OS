"""Golden-task / trajectory evaluation stub tests (mock LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from proagent_os.eval import (
    EvalResult,
    GoldenTask,
    compare_trajectory,
    load_golden_tasks,
    run_golden_task,
)
from proagent_os.eval.golden import maybe_record_eval_metering

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval"
ORCH_FIXTURE_IDS = ("conversion_diagnosis", "research_kb_clock")


def test_compare_trajectory_ordered_subset_pass():
    result = compare_trajectory(
        ["kb_search", "clock", "emit_recommendation"],
        ["kb_search", "clock"],
        task_id="t1",
    )
    assert result.passed
    assert result.match == "ordered_subset"
    assert result.diffs == []
    assert result.actual_tools[-1] == "emit_recommendation"


def test_compare_trajectory_ordered_subset_allows_gaps():
    result = compare_trajectory(
        ["http_fetch", "kb_search", "file_read", "clock"],
        ["kb_search", "clock"],
        task_id="gaps",
    )
    assert result.passed


def test_compare_trajectory_ordered_subset_fail_wrong_order():
    result = compare_trajectory(
        ["clock", "kb_search"],
        ["kb_search", "clock"],
        task_id="order",
    )
    assert not result.passed
    assert result.diffs
    assert "clock" in result.diffs[0] or "missing" in result.diffs[0]


def test_compare_trajectory_ordered_subset_fail_missing():
    result = compare_trajectory(
        ["kb_search"],
        ["kb_search", "clock"],
        task_id="missing",
    )
    assert not result.passed
    assert any("clock" in d for d in result.diffs)


def test_compare_rejects_unknown_match_mode():
    result = compare_trajectory(["a"], ["a"], match="exact", task_id="x")
    assert not result.passed
    assert "unsupported" in result.diffs[0]


def test_load_golden_tasks_from_fixtures():
    tasks = load_golden_tasks(FIXTURES)
    ids = {t.id for t in tasks}
    assert "conversion_diagnosis" in ids
    assert "research_kb_clock" in ids
    assert "synthetic_trajectory_only" in ids
    conv = next(t for t in tasks if t.id == "conversion_diagnosis")
    assert conv.match == "ordered_subset"
    assert conv.expected_tools == ["kb_search", "clock", "emit_recommendation"]
    assert conv.seed_kb


def test_golden_task_from_mapping_requires_id():
    with pytest.raises(ValueError, match="missing id"):
        GoldenTask.from_mapping({"task": "x", "expected_tools": []})


def test_maybe_record_eval_metering_is_stub():
    meta = maybe_record_eval_metering("ten_x", task_id="t", passed=True)
    assert meta is not None
    assert meta["recorded"] is False
    assert meta["billing"] == "stub"
    assert maybe_record_eval_metering(None, task_id="t", passed=True) is None


def test_eval_result_to_dict():
    r = EvalResult(
        task_id="t",
        passed=True,
        match="ordered_subset",
        expected_tools=["a"],
        actual_tools=["a"],
    )
    d = r.to_dict()
    assert d["passed"] is True
    assert d["expected_tools"] == ["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_id", ORCH_FIXTURE_IDS)
async def test_run_golden_fixtures_with_mock_orchestrator(fixture_id: str):
    tasks = {t.id: t for t in load_golden_tasks(FIXTURES)}
    golden = tasks[fixture_id]
    result = await run_golden_task(golden)
    assert isinstance(result, EvalResult)
    assert result.task_id == fixture_id
    assert result.match == "ordered_subset"
    assert result.passed, f"diffs={result.diffs} actual={result.actual_tools}"
    assert result.summary.get("orchestrator_status") == "completed"
    assert result.summary.get("eval") == "stub"
    assert result.metering is not None
    assert result.metering["recorded"] is False
