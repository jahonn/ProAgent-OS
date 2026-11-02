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
            "hitl_checkpoint_json": "{}",
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


@pytest.mark.asyncio
async def test_orchestrator_resume_continues_specialist_loop(monkeypatch):
    """Approve after request_run_approval re-enters specialist + finishes (mock)."""
    tid = _tenant("pk_hitl_continue")
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
            tool_names = []
            if tools:
                for t in tools:
                    fn = t.get("function") if isinstance(t, dict) else None
                    if isinstance(fn, dict) and fn.get("name"):
                        tool_names.append(fn["name"])
            # Planner (no tools)
            if not tools:
                # First plan call vs later aggregate: distinguish by content.
                user = ""
                for m in messages:
                    if m.role == "user" and m.content:
                        user = m.content
                        break
                if "Specialist results" in user:
                    return LLMResponse(
                        content="Final: email approved and drafted.",
                        model="scripted",
                    )
                return LLMResponse(
                    content='{"steps":[{"agent":"analyst","goal":"gate","tools":["request_run_approval"]}],"summary":"hitl"}',
                    model="scripted",
                )
            # Specialist: first time request approval; after HITL resume seed, answer text.
            joined = " ".join(
                (m.content or "") for m in messages if isinstance(m.content, str)
            )
            if "A human approved the pending action" in joined:
                return LLMResponse(
                    content="Draft prepared after human approval.",
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
            return LLMResponse(content="unexpected", model="scripted")

    monkeypatch.setattr(svc.orchestrator, "llm", ScriptedLLM())
    paused = await svc.orchestrator.run_task(tid, "Send a consequential email")
    assert paused["status"] == STATUS_AWAITING_HUMAN
    assert paused.get("hitl_checkpoint", {}).get("v") == 1

    resumed = await svc.orchestrator.resume_after_hitl(
        tid, paused["task_id"], actor="ops", note="ship it"
    )
    assert resumed["status"] == STATUS_COMPLETED
    assert resumed.get("hitl") == "continued"
    assert resumed.get("hitl_decision") == "approved"
    assert "approved" in (resumed.get("result") or "").lower() or "Draft" in (
        resumed.get("result") or ""
    )
    row = get_db().fetchone(
        "SELECT status, hitl_decision, hitl_checkpoint_json FROM tasks WHERE id=?",
        (paused["task_id"],),
    )
    assert row["status"] == STATUS_COMPLETED
    assert row["hitl_decision"] == "approved"
    assert row["hitl_checkpoint_json"] in ("{}", "")


@pytest.mark.asyncio
async def test_resume_continue_flag_off_marks_completed_without_reloop(monkeypatch):
    """PROAGENT_HITL_RESUME_CONTINUE=false restores legacy stub approve."""
    from proagent_os.config import get_settings
    from proagent_os.services import reset_services

    monkeypatch.setenv("PROAGENT_HITL_RESUME_CONTINUE", "false")
    get_settings.cache_clear()
    reset_services()

    tid = _tenant("pk_hitl_stub")
    svc = get_services()

    class ScriptedLLM:
        async def chat(
            self,
            messages: List[LLMMessage],
            tools: Optional[List[Dict[str, Any]]] = None,
            tool_choice: Any = None,
        ) -> LLMResponse:
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
                            id="tc_hitl_2",
                            name="request_run_approval",
                            arguments={"reason": "Confirm", "action": {"kind": "x"}},
                            raw_arguments="",
                        )
                    ],
                )
            return LLMResponse(content="should not continue", model="scripted")

    monkeypatch.setattr(svc.orchestrator, "llm", ScriptedLLM())
    paused = await svc.orchestrator.run_task(tid, "Need confirm")
    assert paused["status"] == STATUS_AWAITING_HUMAN
    resumed = await svc.orchestrator.resume_after_hitl(
        tid, paused["task_id"], actor="ops", note="legacy"
    )
    assert resumed["status"] == STATUS_COMPLETED
    assert resumed.get("hitl") != "continued"
    assert "legacy" in (resumed.get("result") or "")


@pytest.mark.asyncio
async def test_api_resume_continues_when_checkpoint_present(monkeypatch):
    """HTTP resume path uses orchestrator continue (async)."""
    tid = _tenant("pk_hitl_api_cont")
    svc = get_services()

    class ScriptedLLM:
        async def chat(
            self,
            messages: List[LLMMessage],
            tools: Optional[List[Dict[str, Any]]] = None,
            tool_choice: Any = None,
        ) -> LLMResponse:
            tool_names = []
            if tools:
                for t in tools:
                    fn = t.get("function") if isinstance(t, dict) else None
                    if isinstance(fn, dict) and fn.get("name"):
                        tool_names.append(fn["name"])
            if not tools:
                user = ""
                for m in messages:
                    if m.role == "user" and m.content:
                        user = m.content
                        break
                if "Specialist results" in user:
                    return LLMResponse(content="Aggregated after HITL.", model="scripted")
                return LLMResponse(
                    content='{"steps":[{"agent":"analyst","goal":"gate","tools":["request_run_approval"]}],"summary":"hitl"}',
                    model="scripted",
                )
            joined = " ".join(
                (m.content or "") for m in messages if isinstance(m.content, str)
            )
            if "A human approved the pending action" in joined:
                return LLMResponse(content="Continued after approve.", model="scripted")
            if "request_run_approval" in tool_names:
                return LLMResponse(
                    content=None,
                    model="scripted",
                    tool_calls=[
                        ToolCall(
                            id="tc_api",
                            name="request_run_approval",
                            arguments={"reason": "Confirm publish", "action": {"kind": "publish"}},
                            raw_arguments="",
                        )
                    ],
                )
            return LLMResponse(content="?", model="scripted")

    monkeypatch.setattr(svc.orchestrator, "llm", ScriptedLLM())
    paused = await svc.orchestrator.run_task(tid, "Publish")
    assert paused["status"] == STATUS_AWAITING_HUMAN
    app = create_app()
    client = TestClient(app)
    headers = {"Authorization": "Bearer pk_hitl_api_cont"}
    set_tenant_role(tid, Role.TENANT_OPERATOR.value)
    resumed = client.post(
        f"/api/v1/tasks/{paused['task_id']}/resume",
        headers=headers,
        json={"note": "ok"},
    )
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["status"] == STATUS_COMPLETED
    assert body.get("hitl") == "continued"
