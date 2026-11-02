from __future__ import annotations

import pytest

from proagent_os.harness.loop import (
    LoopPhase,
    OUTER_LOOP_PHASES,
    assert_known_phases,
    event,
    phase_counts,
    phase_value,
    summarize_run,
    tool_trajectory,
)


def test_loop_phase_wire_values_stable():
    assert LoopPhase.PLAN.value == "plan"
    assert LoopPhase.SPECIALIST.value == "specialist"
    assert LoopPhase.TOOL.value == "tool"
    assert LoopPhase.AGGREGATE.value == "aggregate"
    assert [p.value for p in OUTER_LOOP_PHASES] == [
        "plan",
        "specialist",
        "tool",
        "aggregate",
    ]


def test_event_and_trajectory_helpers():
    trace = [
        event(LoopPhase.PLAN, summary="s"),
        event(LoopPhase.SPECIALIST, agent="researcher", round=0),
        event(LoopPhase.TOOL, agent="researcher", name="kb_search", ok=True),
        event(LoopPhase.TOOL, agent="researcher", name="clock", ok=True),
        event(LoopPhase.SPECIALIST, agent="analyst", round=0),
        event(LoopPhase.AGGREGATE, model="mock"),
    ]
    assert phase_value(LoopPhase.TOOL) == "tool"
    assert tool_trajectory(trace) == ["kb_search", "clock"]
    assert phase_counts(trace)["tool"] == 2
    assert assert_known_phases(trace) is None
    summary = summarize_run(trace)
    assert summary["eval"] == "stub"
    assert summary["tool_trajectory"] == ["kb_search", "clock"]
    assert summary["specialist_agents"] == ["researcher", "analyst"]


def test_assert_known_phases_rejects_unknown():
    bad = [{"phase": "decide"}]
    assert assert_known_phases(bad) is not None
    assert assert_known_phases(bad, allow_unknown=True) is None


@pytest.mark.asyncio
async def test_orchestrator_trace_uses_loop_phases():
    from proagent_os.db import get_db, new_id, utcnow
    from proagent_os.services import get_services

    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Loop Co",
            "api_key": "pk_loop",
            "goals": "Test loop phases",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    svc = get_services()
    svc.kb.ingest_text(
        tid,
        "manual",
        "Product",
        "Acme conversion rate is below target at 1.8 percent.",
    )
    result = await svc.orchestrator.run_task(
        tid, "Why is conversion low and what should we do?"
    )
    assert result["status"] == "completed"
    assert assert_known_phases(result["trace"]) is None
    assert any(t["phase"] == LoopPhase.PLAN.value for t in result["trace"])
    assert any(t["phase"] == LoopPhase.AGGREGATE.value for t in result["trace"])
    traj = tool_trajectory(result["trace"])
    assert len(traj) >= 1
    summary = summarize_run(result["trace"])
    assert summary["eval"] == "stub"
    assert summary["tool_trajectory"] == traj
