"""Tests for plan-and-replan outer loop stub."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from proagent_os.config import get_settings
from proagent_os.db import get_db, new_id, utcnow
from proagent_os.harness.loop import LoopPhase, assert_known_phases, event
from proagent_os.harness.orchestrator import Orchestrator
from proagent_os.harness.replan import observations_summary, should_replan, step_looks_failed
from proagent_os.services import get_services, reset_services


def _use(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_services()


def _tenant() -> str:
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Replan Co",
            "api_key": "pk_replan",
            "goals": "Test replan",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    return tid


def test_should_replan_disabled():
    ok, reason = should_replan(
        {"steps": []},
        [],
        enabled=False,
        attempt=0,
        max_replans=1,
        force=True,
    )
    assert ok is False
    assert reason == "disabled"


def test_should_replan_empty_plan():
    ok, reason = should_replan(
        {"steps": []},
        [],
        enabled=True,
        attempt=0,
        max_replans=1,
    )
    assert ok is True
    assert reason == "empty_plan"


def test_should_replan_all_failed_and_empty_inputs():
    ok, reason = should_replan(
        {"steps": [{"agent": "researcher"}]},
        [{"agent": "researcher", "goal": "g", "result": ""}],
        enabled=True,
        attempt=0,
        max_replans=2,
    )
    assert ok is True
    assert reason == "empty_aggregate_inputs"

    ok2, reason2 = should_replan(
        {"steps": [{"agent": "researcher"}]},
        [{"agent": "researcher", "goal": "g", "result": "[error] boom"}],
        enabled=True,
        attempt=0,
        max_replans=2,
    )
    assert ok2 is True
    assert reason2 == "all_steps_failed"


def test_should_replan_max_cap_and_sufficient():
    ok, reason = should_replan(
        {"steps": []},
        [],
        enabled=True,
        attempt=1,
        max_replans=1,
        force=True,
    )
    assert ok is False
    assert reason == "max_reached"

    ok2, reason2 = should_replan(
        {"steps": [{"agent": "researcher"}]},
        [{"agent": "researcher", "goal": "g", "result": "useful findings"}],
        enabled=True,
        attempt=0,
        max_replans=1,
    )
    assert ok2 is False
    assert reason2 == "sufficient"


def test_step_looks_failed_and_observations_summary():
    assert step_looks_failed({"result": ""}) is True
    assert step_looks_failed({"result": "ok", "failed": True}) is True
    assert step_looks_failed({"result": "useful"}) is False
    summary = observations_summary(
        {"steps": [{"agent": "researcher", "goal": "g"}], "summary": "s"},
        [{"agent": "researcher", "goal": "g", "result": "findings"}],
    )
    assert "prior_plan" in summary
    assert "findings" in summary


@pytest.mark.asyncio
async def test_orchestrator_replan_disabled_no_extra_plan(monkeypatch: pytest.MonkeyPatch):
    _use(
        monkeypatch,
        PROAGENT_REPLAN_ENABLED="false",
        PROAGENT_REPLAN_FORCE="true",
        PROAGENT_REPLAN_MAX="3",
    )
    tid = _tenant()
    svc = get_services()
    svc.kb.ingest_text(tid, "manual", "Product", "Conversion is 1.8 percent.")

    calls: List[LoopPhase] = []
    real_plan = Orchestrator._plan

    async def counting_plan(self, *args, **kwargs):
        phase = kwargs.get("phase", LoopPhase.PLAN)
        calls.append(phase)
        return await real_plan(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "_plan", counting_plan)
    result = await svc.orchestrator.run_task(tid, "Why is conversion low?")
    assert result["status"] == "completed"
    assert calls == [LoopPhase.PLAN]
    assert not any(t["phase"] == LoopPhase.REPLAN.value for t in result["trace"])
    assert assert_known_phases(result["trace"]) is None


@pytest.mark.asyncio
async def test_orchestrator_replan_force_fires_once(monkeypatch: pytest.MonkeyPatch):
    _use(
        monkeypatch,
        PROAGENT_REPLAN_ENABLED="true",
        PROAGENT_REPLAN_FORCE="true",
        PROAGENT_REPLAN_MAX="1",
    )
    tid = _tenant()
    svc = get_services()
    svc.kb.ingest_text(tid, "manual", "Product", "Conversion is 1.8 percent.")

    plan_phases: List[LoopPhase] = []
    real_plan = Orchestrator._plan

    async def counting_plan(self, *args, **kwargs):
        phase = kwargs.get("phase", LoopPhase.PLAN)
        plan_phases.append(phase)
        return await real_plan(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "_plan", counting_plan)
    result = await svc.orchestrator.run_task(tid, "Why is conversion low?")
    assert result["status"] == "completed"
    assert plan_phases == [LoopPhase.PLAN, LoopPhase.REPLAN]
    replan_rows = [t for t in result["trace"] if t["phase"] == LoopPhase.REPLAN.value]
    assert len(replan_rows) >= 1
    # Decision event + planner output both use REPLAN phase.
    assert any(r.get("reason") == "force" for r in replan_rows)
    assert any(r.get("replan") is True for r in replan_rows)
    assert assert_known_phases(result["trace"]) is None


@pytest.mark.asyncio
async def test_orchestrator_replan_max_cap_respected(monkeypatch: pytest.MonkeyPatch):
    _use(
        monkeypatch,
        PROAGENT_REPLAN_ENABLED="true",
        PROAGENT_REPLAN_FORCE="true",
        PROAGENT_REPLAN_MAX="2",
    )
    tid = _tenant()
    svc = get_services()
    svc.kb.ingest_text(tid, "manual", "Product", "Conversion is 1.8 percent.")

    plan_phases: List[LoopPhase] = []
    real_plan = Orchestrator._plan

    async def counting_plan(self, *args, **kwargs):
        phase = kwargs.get("phase", LoopPhase.PLAN)
        plan_phases.append(phase)
        return await real_plan(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "_plan", counting_plan)
    result = await svc.orchestrator.run_task(tid, "Why is conversion low?")
    assert result["status"] == "completed"
    # Initial plan + 2 replans (max)
    assert plan_phases == [LoopPhase.PLAN, LoopPhase.REPLAN, LoopPhase.REPLAN]
    decision_events = [
        t
        for t in result["trace"]
        if t["phase"] == LoopPhase.REPLAN.value and t.get("reason") == "force"
    ]
    assert len(decision_events) == 2
    assert {e["attempt"] for e in decision_events} == {1, 2}


@pytest.mark.asyncio
async def test_orchestrator_replan_on_empty_plan_fixture(monkeypatch: pytest.MonkeyPatch):
    _use(
        monkeypatch,
        PROAGENT_REPLAN_ENABLED="true",
        PROAGENT_REPLAN_FORCE="false",
        PROAGENT_REPLAN_MAX="1",
    )
    tid = _tenant()
    svc = get_services()
    svc.kb.ingest_text(tid, "manual", "Product", "Conversion is 1.8 percent.")

    calls = {"n": 0}
    real_plan = Orchestrator._plan

    async def empty_then_real(
        self,
        task: str,
        brand_block: str,
        trace: List[Dict[str, Any]],
        *,
        prior_context: str = "",
        phase: LoopPhase = LoopPhase.PLAN,
        prior_plan: Optional[Dict[str, Any]] = None,
        observations: str = "",
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            parsed = {"steps": [], "summary": "fixture empty plan"}
            trace.append(event(phase, model="fixture-empty", parsed=parsed, replan=False))
            return parsed
        return await real_plan(
            self,
            task,
            brand_block,
            trace,
            prior_context=prior_context,
            phase=phase,
            prior_plan=prior_plan,
            observations=observations,
        )

    monkeypatch.setattr(Orchestrator, "_plan", empty_then_real)
    result = await svc.orchestrator.run_task(tid, "Why is conversion low?")
    assert result["status"] == "completed"
    assert calls["n"] == 2
    assert any(
        t["phase"] == LoopPhase.REPLAN.value and t.get("reason") == "empty_plan"
        for t in result["trace"]
    )
    assert result["plan"].get("steps")
    assert assert_known_phases(result["trace"]) is None
