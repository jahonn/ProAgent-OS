from __future__ import annotations

import pytest

from proagent_os.professional.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from proagent_os.professional.tools.builtins import (
    ClockTool,
    HttpFetchTool,
    KbSearchTool,
    build_builtin_registry,
)
from proagent_os.professional.tools.validation import validate_arguments


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "count": {"type": "integer"},
            "flag": {"type": "boolean"},
            "meta": {"type": "object"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    async def run(self, arguments, ctx):  # noqa: ANN001
        return ToolResult(ok=True, output={"echo": arguments.get("text")}, error=None)


@pytest.mark.asyncio
async def test_valid_args_pass_and_execute():
    reg = ToolRegistry(strict_schema=True)
    reg.register(_EchoTool())
    ctx = ToolContext(tenant_id="t1")
    result = await reg.invoke("echo", {"text": "hi", "count": 2, "flag": True}, ctx)
    assert result.ok is True
    assert result.output == {"echo": "hi"}


@pytest.mark.asyncio
async def test_missing_required_fails_without_run():
    ran = {"n": 0}

    class Guard(Tool):
        name = "guard"
        description = "g"
        parameters = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }

        async def run(self, arguments, ctx):  # noqa: ANN001
            ran["n"] += 1
            return ToolResult(ok=True, output="ran", error=None)

    reg = ToolRegistry(strict_schema=True)
    reg.register(Guard())
    result = await reg.invoke("guard", {}, ToolContext(tenant_id="t1"))
    assert result.ok is False
    assert ran["n"] == 0
    assert result.output["error"] == "schema_validation_failed"
    assert result.output["executed"] is False
    assert any(i["code"] == "required" for i in result.output["issues"])
    assert "url" in (result.error or "")


@pytest.mark.asyncio
async def test_wrong_type_fails_cleanly():
    reg = ToolRegistry(strict_schema=True)
    reg.register(_EchoTool())
    result = await reg.invoke("echo", {"text": 123}, ToolContext(tenant_id="t1"))
    assert result.ok is False
    assert result.output["error"] == "schema_validation_failed"
    assert any(i["code"] == "type" for i in result.output["issues"])


@pytest.mark.asyncio
async def test_additional_properties_false_rejects_extras():
    reg = ToolRegistry(strict_schema=True)
    reg.register(_EchoTool())
    result = await reg.invoke(
        "echo", {"text": "ok", "surprise": True}, ToolContext(tenant_id="t1")
    )
    assert result.ok is False
    assert any(i["code"] == "additionalProperties" for i in result.output["issues"])


@pytest.mark.asyncio
async def test_unknown_tool_still_handled():
    reg = ToolRegistry(strict_schema=True)
    result = await reg.invoke("nope", {"a": 1}, ToolContext(tenant_id="t1"))
    assert result.ok is False
    assert result.error == "Unknown tool: nope"
    assert result.output is None


@pytest.mark.asyncio
async def test_strict_off_skips_validation():
    ran = {"n": 0}

    class Loose(Tool):
        name = "loose"
        description = "l"
        parameters = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }

        async def run(self, arguments, ctx):  # noqa: ANN001
            ran["n"] += 1
            return ToolResult(ok=True, output="ran", error=None)

    reg = ToolRegistry(strict_schema=False)
    reg.register(Loose())
    result = await reg.invoke("loose", {}, ToolContext(tenant_id="t1"))
    assert result.ok is True
    assert ran["n"] == 1


def test_validate_arguments_number_and_array():
    schema = {
        "type": "object",
        "properties": {
            "score": {"type": "number"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["score"],
    }
    ok = validate_arguments({"score": 1.5, "tags": ["a", "b"]}, schema)
    assert ok.ok
    bad = validate_arguments({"score": "x"}, schema)
    assert not bad.ok
    bad_items = validate_arguments({"score": 1, "tags": [1]}, schema)
    assert not bad_items.ok


@pytest.mark.asyncio
async def test_builtin_kb_search_and_http_fetch_schemas():
    reg = build_builtin_registry(strict_schema=True)
    ctx = ToolContext(tenant_id="t1")
    missing = await reg.invoke("kb_search", {}, ctx)
    assert missing.ok is False
    assert missing.output["executed"] is False

    wrong = await reg.invoke("http_fetch", {"url": 99}, ctx)
    assert wrong.ok is False
    # Must not attempt network / fixture read
    assert wrong.output["error"] == "schema_validation_failed"

    clock = await reg.invoke("clock", {}, ctx)
    assert clock.ok is True
    assert "utc" in clock.output


@pytest.mark.asyncio
async def test_mock_orchestrator_path_still_green():
    """Regression: mock multi-tool loop remains green with strict schema on."""
    from proagent_os.db import get_db, new_id, utcnow
    from proagent_os.services import get_services

    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Schema Co",
            "api_key": "pk_schema",
            "goals": "Test",
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
    # Ensure registry is strict (default).
    assert svc.orchestrator.tools.strict_schema is True
    result = await svc.orchestrator.run_task(tid, "Why is conversion low?")
    assert result["status"] == "completed"
    tool_events = [t for t in result["trace"] if t.get("phase") == "tool"]
    assert len(tool_events) >= 1
    assert all(not t.get("schema_validation_failed") for t in tool_events)


def test_builtin_parameter_shapes():
    assert "url" in HttpFetchTool.parameters["properties"]
    assert "query" in KbSearchTool.parameters["required"]
    assert ClockTool.parameters.get("type") == "object"
