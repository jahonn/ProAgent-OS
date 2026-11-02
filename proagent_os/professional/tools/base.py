from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


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
    # invoking ``run``. Arguments are stored as opaque pending_action JSON only.
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
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

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

    async def invoke(self, name: str, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if not tool:
            return ToolResult(ok=False, output=None, error=f"Unknown tool: {name}")
        try:
            return await tool.run(arguments or {}, ctx)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output=None, error=str(exc))
