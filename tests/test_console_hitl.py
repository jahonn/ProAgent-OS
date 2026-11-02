"""Console HITL surface: template strings, and the mock approval prompt."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from proagent_os.llm.base import LLMMessage
from proagent_os.llm.mock import MOCK_HITL_DEMO_MARKER, MockLLMProvider
from proagent_os.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_console_home_renders_hitl_strings() -> None:
    with _client() as client:
        page = client.get("/")
        script = client.get("/static/app.js")
        style = client.get("/static/app.css")
    assert page.status_code == 200, page.text
    html = page.text
    assert "awaiting_human" in html
    assert "Resume" in html
    assert "Reject" in html
    assert "hitl_execute_receipts" in html
    assert "Raw arguments are not shown" in html
    assert "Run mock approval" in html
    assert script.status_code == 200
    js = script.text
    assert MOCK_HITL_DEMO_MARKER in js
    assert 'value="resume"' in js
    assert 'value="reject"' in js
    assert "tenant_operator" in js
    assert "hitl_execute_receipts" in js
    assert "receipt.tool_name" in js
    assert "receipt.observation" not in js
    assert "receipt.arguments" not in js
    assert style.status_code == 200
    assert ".status.awaiting_human" in style.text


@pytest.mark.asyncio
async def test_mock_hitl_marker_is_opt_in() -> None:
    """The demo marker pauses. A normal analyst prompt still emits a recommendation."""
    llm = MockLLMProvider()
    tools = [
        {"type": "function", "function": {"name": "emit_recommendation", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "request_run_approval", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "kb_search", "parameters": {"type": "object"}}},
    ]
    paused = await llm.chat(
        [
            LLMMessage(role="system", content="You are the Analyst specialist."),
            LLMMessage(
                role="user",
                content=f"Overall task: check {MOCK_HITL_DEMO_MARKER}\nYour goal: pause",
            ),
        ],
        tools=tools,
    )
    assert paused.tool_calls and paused.tool_calls[0].name == "request_run_approval"
    plain = await llm.chat(
        [
            LLMMessage(role="system", content="You are the Analyst specialist."),
            LLMMessage(role="user", content="Overall task: Why is conversion low?\nYour goal: analyze"),
        ],
        tools=tools,
    )
    assert plain.tool_calls and plain.tool_calls[0].name == "emit_recommendation"
    plan = await llm.chat(
        [
            LLMMessage(role="system", content="You are the Planner agent. Create a short plan."),
            LLMMessage(role="user", content=f"Task: {MOCK_HITL_DEMO_MARKER}"),
        ]
    )
    parsed = json.loads(plan.content or "")
    assert [step["agent"] for step in parsed["steps"]] == ["analyst"]
    normal = await llm.chat(
        [
            LLMMessage(role="system", content="You are the Planner agent. Create a short plan."),
            LLMMessage(role="user", content="Task: Why is conversion low?"),
        ]
    )
    normal_plan = json.loads(normal.content or "")
    assert [step["agent"] for step in normal_plan["steps"]] == ["researcher", "analyst"]


def test_mock_approval_prompt_resume_and_reject_via_api() -> None:
    """Same prompt the console button sends. Mock provider, no live model."""
    with _client() as client:
        created = client.post(
            "/api/v1/tenants",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"name": "Console HITL", "goals": "demo", "tone": "concise"},
        )
        assert created.status_code == 200, created.text
        key = created.json()["api_key"]
        headers = {"Authorization": f"Bearer {key}"}
        me = client.get("/api/v1/tenants/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["role"] == "tenant_operator"

        def start() -> str:
            started = client.post(
                "/api/v1/tasks",
                headers=headers,
                json={
                    "input": (
                        "Pause this run for human approval before continuing. "
                        f"{MOCK_HITL_DEMO_MARKER}"
                    )
                },
            )
            assert started.status_code == 200, started.text
            body = started.json()
            assert body["status"] == "awaiting_human"
            assert body["pause_reason"]
            return body["task_id"]

        approved_id = start()
        listed = client.get("/api/v1/tasks", headers=headers)
        assert listed.status_code == 200
        item = next(row for row in listed.json() if row["id"] == approved_id)
        assert item["status"] == "awaiting_human"
        assert item["hitl_execute_receipts"] == []

        resumed = client.post(
            f"/api/v1/tasks/{approved_id}/resume",
            headers=headers,
            json={"note": "ok"},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "completed"
        detail = client.get(f"/api/v1/tasks/{approved_id}", headers=headers)
        assert detail.status_code == 200
        receipts = detail.json()["hitl_execute_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["tool_name"] == "request_run_approval"
        assert receipts[0]["status"] == "succeeded"
        assert "arguments" not in receipts[0]
        assert "args" not in receipts[0]

        rejected_id = start()
        rejected = client.post(
            f"/api/v1/tasks/{rejected_id}/reject",
            headers=headers,
            json={"note": "no"},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "cancelled"
        after_reject = client.get(f"/api/v1/tasks/{rejected_id}", headers=headers)
        assert after_reject.json()["hitl_execute_receipts"] == []
        assert after_reject.json()["status"] == "cancelled"
