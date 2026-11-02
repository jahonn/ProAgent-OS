from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import httpx

from proagent_os.professional.kb.store import KnowledgeBase
from proagent_os.professional.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


class HttpFetchTool(Tool):
    name = "http_fetch"
    description = "Fetch a public HTTP(S) URL and return truncated text content."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP(S) URL or fixture://name"},
        },
        "required": ["url"],
    }

    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = arguments.get("url")
        if not url:
            return ToolResult(ok=False, output=None, error="url required")
        if url.startswith("fixture://"):
            name = url.replace("fixture://", "")
            path = Path("fixtures") / name
            if not path.exists():
                return ToolResult(ok=False, output=None, error=f"fixture not found: {name}")
            text = path.read_text(encoding="utf-8")
            return ToolResult(ok=True, output={"url": url, "status": 200, "text": text[:8000]})
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "ProAgentOS/0.1"})
            text = resp.text[:8000]
            return ToolResult(ok=True, output={"url": url, "status": resp.status_code, "text": text})


class FileReadTool(Tool):
    name = "file_read"
    description = "Read a local text/markdown file under fixtures/ or data/ (sandboxed)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path under allowed roots"},
        },
        "required": ["path"],
    }

    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = arguments.get("path", "")
        path = Path(path_str)
        allowed_roots = [Path("fixtures").resolve(), Path("data").resolve(), Path("skills").resolve()]
        try:
            resolved = path.resolve()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output=None, error=str(exc))
        if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
            return ToolResult(ok=False, output=None, error="path outside allowed roots")
        if not resolved.exists() or not resolved.is_file():
            return ToolResult(ok=False, output=None, error="file not found")
        if resolved.suffix.lower() not in {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".html"}:
            return ToolResult(ok=False, output=None, error="unsupported file type")
        return ToolResult(ok=True, output={"path": str(path), "content": resolved.read_text(encoding="utf-8")[:20000]})


class KbSearchTool(Tool):
    name = "kb_search"
    description = "Search the tenant knowledge base (BM25 by default; honors PROAGENT_KB_RETRIEVAL)."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }

    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = arguments.get("query", "")
        top_k = int(arguments.get("top_k", 5))
        kb = KnowledgeBase()
        hits = kb.search(ctx.tenant_id, query, top_k=top_k)
        return ToolResult(ok=True, output={"query": query, "hits": hits})


class ClockTool(Tool):
    name = "clock"
    description = "Return the current UTC timestamp."
    parameters = {"type": "object", "properties": {}}

    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        now = datetime.now(timezone.utc)
        return ToolResult(
            ok=True,
            output={"utc": now.isoformat(), "unix": int(now.timestamp()), "timezone": "UTC"},
        )


class EmitRecommendationTool(Tool):
    name = "emit_recommendation"
    description = "Propose an actionable recommendation for human-in-the-loop approval."
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "rationale": {"type": "string"},
            "action": {"type": "object"},
        },
        "required": ["title"],
    }

    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from proagent_os.proactive.recommendations import RecommendationService

        title = arguments.get("title") or "Untitled recommendation"
        rationale = arguments.get("rationale") or ""
        raw_action = arguments.get("action") or {}
        action = raw_action if isinstance(raw_action, dict) else {}
        rec = RecommendationService().create(
            ctx.tenant_id,
            title=title,
            rationale=rationale,
            action=action,
            monitor_id=ctx.monitor_id,
            task_id=ctx.task_id,
        )
        return ToolResult(
            ok=True,
            output={"recommendation_id": rec["id"], "status": rec.get("status", "pending"), "title": title},
        )


class RequestRunApprovalTool(Tool):
    """Ask a human to confirm before the task run continues (run-level HITL).

    Distinct from ``emit_recommendation`` (proactive inbox). The orchestrator
    honors ``requires_human_approval`` and pauses without treating ``action`` as
    a URL or shell command. ``run`` is a safety fallback only.
    """

    name = "request_run_approval"
    description = (
        "Pause this task run and ask a human to approve or reject a consequential "
        "action before continuing. Prefer this for run interruptions; use "
        "emit_recommendation for the separate proactive inbox."
    )
    requires_human_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why human confirmation is needed"},
            "action": {
                "type": "object",
                "description": "Opaque proposed action JSON (never executed as URL/shell)",
            },
        },
        "required": ["reason"],
    }

    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Orchestrator should pause before invoke. If we get here, still do not execute.
        reason = arguments.get("reason") or "human_approval_required"
        raw_action = arguments.get("action") or {}
        action = raw_action if isinstance(raw_action, dict) else {"_dropped_non_object": True}
        return ToolResult(
            ok=True,
            output={
                "needs_human_approval": True,
                "reason": reason,
                "action": action,
                "task_id": ctx.task_id,
                "executed": False,
            },
        )


def build_builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        HttpFetchTool(),
        FileReadTool(),
        KbSearchTool(),
        ClockTool(),
        EmitRecommendationTool(),
        RequestRunApprovalTool(),
    ):
        registry.register(tool)
    return registry
