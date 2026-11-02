"""Task-run HITL pause / resume / reject stub (mock LLM)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from proagent_os.db import dumps, get_db, new_id, utcnow
from proagent_os.harness.hitl import (
    STATUS_AWAITING_HUMAN,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    sanitize_pending_action,
)
from proagent_os.harness.loop import LoopPhase
from proagent_os.llm.base import LLMMessage, LLMResponse, ToolCall
from proagent_os.main import create_app
from proagent_os.rbac import Role, set_tenant_role
from proagent_os.services import get_services


def _tenant(api_key: str = "pk_hitl") -> str:
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "HITL Co",
            "api_key": api_key,
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
            "key_status": "active",
        },
    )
    return tid


def _seed_running_task(tenant_id: str) -> str:
    task_id = new_id("task")
    now = utcnow()
    get_db().insert(
        "tasks",
        {
            "id": task_id,
            "tenant_id": tenant_id,
            "conversation_id": None,
            "status": STATUS_RUNNING,
            "input": "Need human confirm",
            "plan_json": "[]",
            "result": "",
            "trace_json": "[]",
            "created_at": now,
            "updated_at": now,
            "pause_reason": "",
            "pending_action_json": "{}",
            "hitl_decision": "",
            "hitl_decided_at": "",
        },
    )
    return task_id


def test_sanitize_pending_action_never_executes():
    assert sanitize_pending_action(None) == {}
    assert sanitize_pending_action("https://evil.example/x")["_dropped_non_object"] is True
    assert sanitize_pending_action(["rm", "-rf"])["_dropped_non_object"] is True
    safe = sanitize_pending_action({"kind": "refund", "amount": 12, "url": "https://x"})
    assert safe["kind"] == "refund"
    assert safe["url"] == "https://x"  # stored as data only


def test_pause_resume_reject_service_transitions():
    tid = _tenant()
    hitl = get_services().hitl
    task_id = _seed_running_task(tid)

    paused = hitl.pause_task(
        tid,
        task_id,
        reason="Confirm refund",
        pending_action={"kind": "refund", "amount": 42},
        actor="test",
    )
    assert paused["status"] == STATUS_AWAITING_HUMAN
    assert paused["pause_reason"] == "Confirm refund"
    assert paused["pending_action"]["amount"] == 42
    assert any(e.get("phase") == LoopPhase.HITL.value for e in paused["trace"])

    # Idempotent pause while already awaiting
    again = hitl.pause_task(tid, task_id, reason="ignored")
    assert again["status"] == STATUS_AWAITING_HUMAN
    assert again["pause_reason"] == "Confirm refund"

    resumed = hitl.resume_task(tid, task_id, actor="ops", note="ok to proceed")
    assert resumed["status"] == STATUS_COMPLETED
    assert resumed["hitl_decision"] == "approved"
    assert resumed["hitl_decided_at"]
    assert "ok to proceed" in resumed["result"]

    task_id2 = _seed_running_task(tid)
    hitl.pause_task(tid, task_id2, reason="risky", pending_action={"op": "delete"})
    rejected = hitl.reject_task(tid, task_id2, actor="ops")
    assert rejected["status"] == STATUS_CANCELLED
    assert rejected["hitl_decision"] == "rejected"


@pytest.mark.asyncio
async def test_orchestrator_pauses_on_request_run_approval(monkeypatch):
    """Mock specialist that only requests run approval → awaiting_human."""
    tid = _tenant("pk_orch_hitl")
    svc = get_services()

    class ScriptedLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(
            self,
            messages: List[LLMMessage],
            tools: Optional[List[Dict[str, Any]]] = None,
            tool_choice: Any = None,
        ) -> LLMResponse:
            self.calls += 1
            # First call is planner (no tools) → one specialist step that will HITL.
            tool_names = []
            if tools:
                for t in tools:
                    fn = t.get("function") if isinstance(t, dict) else None
                    if isinstance(fn, dict) and fn.get("name"):
                        tool_names.append(fn["name"])
            if not tools:
                return LLMResponse(
                    content='{"steps":[{"agent":"analyst","goal":"gate","tools":["request_run_approval"]}],"summary":"hitl"}',
                    model="scripted",
                )
            if "request_run_approval" in tool_names:
                return LLMResponse(
                    content=None,
                    model="scripted",
                    tool_calls=[
                        ToolCall(
                            id="tc_hitl_1",
                            name="request_run_approval",
                            arguments={
                                "reason": "Confirm outbound email",
                                "action": {"kind": "email", "to": "user@example.com"},
                            },
                            raw_arguments="",
                        )
                    ],
                )
            return LLMResponse(content="should not aggregate", model="scripted")

    monkeypatch.setattr(svc.orchestrator, "llm", ScriptedLLM())
    result = await svc.orchestrator.run_task(tid, "Send a consequential email")
    assert result["status"] == STATUS_AWAITING_HUMAN
    assert result["pause_reason"] == "Confirm outbound email"
    assert result["pending_action"]["kind"] == "email"
    row = get_db().fetchone("SELECT status, pause_reason FROM tasks WHERE id=?", (result["task_id"],))
    assert row["status"] == STATUS_AWAITING_HUMAN
    # Proactive recommendation path must remain untouched (no auto rec from this tool).
    recs = get_db().fetchall("SELECT id FROM recommendations WHERE tenant_id=?", (tid,))
    assert recs == []


@pytest.mark.asyncio
async def test_emit_recommendation_still_completes_without_run_pause():
    """Existing proactive inbox path must not flip the task to awaiting_human."""
    tid = _tenant("pk_rec_ok")
    svc = get_services()
    svc.kb.ingest_text(tid, "manual", "Product", "Conversion is 1.8 percent.")
    result = await svc.orchestrator.run_task(tid, "Why is conversion low and what should we do?")
    assert result["status"] == STATUS_COMPLETED
    # Mock path typically emits a recommendation; either way task must complete.
    row = get_db().fetchone("SELECT status FROM tasks WHERE id=?", (result["task_id"],))
    assert row["status"] == STATUS_COMPLETED


def test_api_pause_resume_reject_and_readonly_403():
    tid = _tenant("pk_hitl_api")
    task_id = _seed_running_task(tid)
    app = create_app()
    client = TestClient(app)
    headers = {"Authorization": "Bearer pk_hitl_api"}

    paused = client.post(
        f"/api/v1/tasks/{task_id}/pause",
        headers=headers,
        json={"reason": "Need confirm", "pending_action": {"kind": "publish"}},
    )
    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert body["status"] == STATUS_AWAITING_HUMAN
    assert body["pending_action"]["kind"] == "publish"

    set_tenant_role(tid, Role.TENANT_READONLY.value)
    denied_resume = client.post(f"/api/v1/tasks/{task_id}/resume", headers=headers)
    assert denied_resume.status_code == 403
    assert "recommendation.decide" in denied_resume.json()["detail"]
    denied_reject = client.post(f"/api/v1/tasks/{task_id}/reject", headers=headers)
    assert denied_reject.status_code == 403
    still = get_db().fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
    assert still["status"] == STATUS_AWAITING_HUMAN

    set_tenant_role(tid, Role.TENANT_OPERATOR.value)
    resumed = client.post(
        f"/api/v1/tasks/{task_id}/resume",
        headers=headers,
        json={"note": "ship it"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == STATUS_COMPLETED
    assert resumed.json()["hitl_decision"] == "approved"

    task_id2 = _seed_running_task(tid)
    assert (
        client.post(
            f"/api/v1/tasks/{task_id2}/pause",
            headers=headers,
            json={"reason": "nope"},
        ).status_code
        == 200
    )
    rejected = client.post(f"/api/v1/tasks/{task_id2}/reject", headers=headers)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == STATUS_CANCELLED
