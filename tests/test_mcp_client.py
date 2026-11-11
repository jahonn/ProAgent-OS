"""Opt-in MCP stdio adapter. The fake server is a local subprocess, not a network API."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from proagent_os.config import get_settings
from proagent_os.harness.loop import tool_trajectory
from proagent_os.harness.orchestrator import Orchestrator
from proagent_os.harness.registry import AgentRegistry, AgentSpec
from proagent_os.llm.mock import MockLLMProvider
from proagent_os.professional.skills.loader import SkillLoader
from proagent_os.professional.tools.base import ToolContext
from proagent_os.professional.tools.builtins import build_tool_registry
from proagent_os.services import get_services, reset_services

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_fake_server.py"
_MCP_MODULE = "proagent_os.professional.tools.mcp_client"


def _fake_server(**extra: object) -> dict:
    spec = {
        "name": "fake",
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "env": {"PROAGENT_MCP_FAKE_MARK": "marked"},
    }
    spec.update(extra)
    return spec


def test_mcp_disabled_does_not_load_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off: do not import the adapter or start a server, even if servers are set."""
    sys.modules.pop(_MCP_MODULE, None)
    monkeypatch.setenv("PROAGENT_MCP_ENABLED", "false")
    monkeypatch.setenv("PROAGENT_MCP_SERVERS", json.dumps([_fake_server()]))
    get_settings.cache_clear()
    registry = build_tool_registry()
    try:
        assert _MCP_MODULE not in sys.modules
        assert registry.get("mcp_fake_ping") is None
        assert registry.get("clock") is not None
        assert all(not item["name"].startswith("mcp_") for item in registry.list())
    finally:
        registry.close()

    sys.modules.pop(_MCP_MODULE, None)
    get_settings.cache_clear()
    reset_services()
    assert get_services().tools.get("mcp_fake_ping") is None
    assert _MCP_MODULE not in sys.modules


@pytest.mark.asyncio
async def test_mcp_stdio_registers_and_specialist_invokes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the adapter, list the fake tool, invoke it, and run one specialist round."""
    monkeypatch.setenv("PROAGENT_MCP_ENABLED", "true")
    monkeypatch.setenv("PROAGENT_MCP_CALL_TIMEOUT_S", "5")
    monkeypatch.setenv("PROAGENT_MCP_SERVERS", json.dumps([_fake_server()]))
    get_settings.cache_clear()
    svc = reset_services()
    try:
        listed = {item["name"]: item for item in svc.tools.list()}
        assert "mcp_fake_ping" in listed
        assert "mcp_fake_echo" in listed
        assert listed["mcp_fake_echo"]["parameters"]["required"] == ["text"]
        assert listed["mcp_fake_ping"]["description"].startswith("[mcp:fake]")
        assert svc.tools.get("clock") is not None

        ctx = ToolContext(tenant_id="ten_mcp")
        first = await svc.tools.invoke("mcp_fake_ping", {}, ctx)
        assert first.ok is True
        assert first.output["text"] == "pong:1:marked"

        rejected = await svc.tools.invoke("mcp_fake_echo", {}, ctx)
        assert rejected.ok is False
        assert rejected.output["error"] == "schema_validation_failed"
        assert rejected.output["executed"] is False

        second = await svc.tools.invoke("mcp_fake_ping", {}, ctx)
        assert second.ok is True
        assert second.output["text"] == "pong:2:marked"

        echoed = await svc.tools.invoke("mcp_fake_echo", {"text": "hello-mcp"}, ctx)
        assert echoed.ok is True
        assert echoed.output["text"] == "hello-mcp"

        failed = await svc.tools.invoke("mcp_fake_fail", {}, ctx)
        assert failed.ok is False
        assert failed.error == "boom"

        agents = AgentRegistry()
        agents.register(
            AgentSpec(
                id="scout",
                role="specialist",
                tools=["mcp_fake_ping"],
                system_prompt="You are a field scout. Use tools when they help.",
                description="MCP test specialist",
            )
        )
        orchestrator = Orchestrator(
            MockLLMProvider(),
            agents,
            svc.tools,
            SkillLoader([]),
            max_tool_rounds=3,
        )
        trace: list[dict] = []
        answer = await orchestrator._run_specialist(
            "scout",
            "Call the ping tool",
            "Check the MCP server",
            "",
            ctx,
            trace,
        )
        assert "finished" in answer.lower()
        assert tool_trajectory(trace) == ["mcp_fake_ping"]
        tool_rows = [row for row in trace if row.get("phase") == "tool"]
        assert tool_rows[0]["ok"] is True
        assert tool_rows[0]["name"] == "mcp_fake_ping"
        preview = tool_rows[0].get("output_preview") or ""
        assert "pong:5:marked" in preview
        offered = orchestrator.tools.openai_tools(["mcp_fake_ping"])
        assert [item["function"]["name"] for item in offered] == ["mcp_fake_ping"]
    finally:
        svc.tools.close()


def test_unavailable_mcp_server_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROAGENT_MCP_ENABLED", "true")
    monkeypatch.setenv("PROAGENT_MCP_CALL_TIMEOUT_S", "5")
    monkeypatch.setenv(
        "PROAGENT_MCP_SERVERS",
        json.dumps(
            [
                {"name": "missing", "command": str(tmp_path / "no-such-mcp-bin"), "args": []},
                _fake_server(),
            ]
        ),
    )
    get_settings.cache_clear()
    registry = build_tool_registry()
    try:
        assert registry.get("clock") is not None
        assert registry.get("mcp_missing_ping") is None
        assert registry.get("mcp_fake_ping") is not None
    finally:
        registry.close()


def test_non_stdio_transport_does_not_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "spawned"
    monkeypatch.setenv("PROAGENT_MCP_ENABLED", "true")
    monkeypatch.setenv(
        "PROAGENT_MCP_SERVERS",
        json.dumps(
            [
                {
                    "name": "remote",
                    "transport": "http",
                    "url": "http://127.0.0.1:9/mcp",
                    "command": sys.executable,
                    "args": ["-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('x')"],
                }
            ]
        ),
    )
    get_settings.cache_clear()
    registry = build_tool_registry()
    try:
        assert registry.get("mcp_remote_ping") is None
        assert registry.get("clock") is not None
        assert not marker.exists()
    finally:
        registry.close()


def test_invalid_mcp_server_json_keeps_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROAGENT_MCP_ENABLED", "true")
    monkeypatch.setenv("PROAGENT_MCP_SERVERS", "{not-json")
    get_settings.cache_clear()
    registry = build_tool_registry()
    try:
        assert registry.get("kb_search") is not None
        assert all(not item["name"].startswith("mcp_") for item in registry.list())
    finally:
        registry.close()


def test_registry_tool_name_sanitizes() -> None:
    from proagent_os.professional.tools.mcp_client import registry_tool_name

    assert registry_tool_name("fake", "ping") == "mcp_fake_ping"
    assert registry_tool_name("fake", "echo-text") == "mcp_fake_echo_text"
    assert registry_tool_name("!!!", "ping") is None
