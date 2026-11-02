from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from proagent_os.professional.tools.validation import validate_tool_call


@dataclass
class ToolContext:
    tenant_id: str
    task_id: Optional[str] = None
    monitor_id: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    ok: bool
    output: Any
    error: Optional[str] = None


class Tool(ABC):
    name: str
    description: str
    # When True, the orchestrator pauses the task run (awaiting_human) instead of
    # invoking ``run``. The pause stores a structured pending_action snapshot
    # (tool name + validated arguments). On approve, that snapshot may be
    # invoked once through ToolRegistry. Reject never calls ``run``. The
    # snapshot is not a URL or a shell command.
    requires_human_approval: bool = False
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }

    @abstractmethod
    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


class ToolRegistry:
    def __init__(self, *, strict_schema: bool = True) -> None:
        self._tools: Dict[str, Tool] = {}
        # When True, invoke validates arguments against the tool's JSON schema
        # before run (OpenAI FunctionTool / Anthropic tool_use style).
        self.strict_schema = strict_schema

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def openai_tools(self, names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        if names is None:
            return [t.openai_tool() for t in self._tools.values()]
        out: List[Dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool:
                out.append(tool.openai_tool())
        return out

    def validate(
        self,
        name: str,
        arguments: Any,
        *,
        strict: Optional[bool] = None,
    ) -> ToolResult:
        """Validate without executing. Unknown tool → error (same as invoke)."""
        tool = self.get(name)
        if not tool:
            return ToolResult(ok=False, output=None, error=f"Unknown tool: {name}")
        use_strict = self.strict_schema if strict is None else strict
        result, observation = validate_tool_call(
            name,
            arguments,
            tool.parameters,
            strict=use_strict,
        )
        if result.ok:
            return ToolResult(ok=True, output={"valid": True}, error=None)
        return ToolResult(ok=False, output=observation, error=result.error_message())

    async def invoke(self, name: str, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if not tool:
            return ToolResult(ok=False, output=None, error=f"Unknown tool: {name}")
        check = self.validate(name, arguments if arguments is not None else {})
        if not check.ok:
            # Do not execute: no shell-out, no HTTP, no URL follow from bad args.
            return check
        try:
            return await tool.run(arguments or {}, ctx)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output=None, error=str(exc))
