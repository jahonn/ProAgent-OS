from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from proagent_os.config import get_settings
from proagent_os.db import dumps, get_db, new_id, utcnow
from proagent_os.harness.loop import LoopPhase, event
from proagent_os.harness.memory import ConversationMemory
from proagent_os.harness.registry import AgentRegistry
from proagent_os.llm.base import LLMMessage, LLMProvider, LLMResponse, ToolCall
from proagent_os.metering import bind_tenant, unbind_tenant
from proagent_os.professional.brand.store import BrandStore
from proagent_os.professional.skills.loader import SkillLoader
from proagent_os.professional.tools.base import ToolContext, ToolRegistry


def _extract_json(text: Optional[str]) -> Any:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _args_summary(arguments: Dict[str, Any], limit: int = 240) -> str:
    try:
        s = json.dumps(arguments, ensure_ascii=False, default=str)
    except TypeError:
        s = str(arguments)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _legacy_tool_calls_from_content(content: Optional[str]) -> Tuple[List[ToolCall], Optional[str]]:
    """Compat: parse old JSON-in-content {tool_calls, answer} into ToolCall list."""
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        return [], None
    raw_calls = parsed.get("tool_calls") or []
    answer = parsed.get("answer")
    if not raw_calls:
        return [], answer if isinstance(answer, str) else None
    calls: List[ToolCall] = []
    for i, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            continue
        name = call.get("name") or ""
        args = call.get("arguments") or {}
        if not isinstance(args, dict):
            args = {"value": args}
        calls.append(
            ToolCall(
                id=call.get("id") or f"legacy_{i}",
                name=name,
                arguments=args,
                raw_arguments=json.dumps(args, ensure_ascii=False),
            )
        )
    return calls, answer if isinstance(answer, str) else None


class Orchestrator:
    """Planner → Specialist agents → Aggregator run loop.

    Trace phases use :class:`~proagent_os.harness.loop.LoopPhase` (see
    ``docs/ARCHITECTURE.md``). Specialist steps still run an inner
    model→tool→observe loop up to ``max_tool_rounds``.
    """

    def __init__(
        self,
        llm: LLMProvider,
        agents: AgentRegistry,
        tools: ToolRegistry,
        skills: SkillLoader,
        *,
        max_tool_rounds: Optional[int] = None,
    ) -> None:
        self.llm = llm
        self.agents = agents
        self.tools = tools
        self.skills = skills
        self.memory = ConversationMemory()
        self.brand = BrandStore()
        self.db = get_db()
        settings = get_settings()
        self.max_tool_rounds = max_tool_rounds if max_tool_rounds is not None else settings.max_tool_rounds

    async def run_task(
        self,
        tenant_id: str,
        input_text: str,
        *,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a task and attribute LLM usage rows to ``tenant_id``."""
        token = bind_tenant(tenant_id)
        try:
            return await self._run_task(
                tenant_id,
                input_text,
                conversation_id=conversation_id,
            )
        finally:
            unbind_tenant(token)

    async def _run_task(
        self,
        tenant_id: str,
        input_text: str,
        *,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not conversation_id:
            conversation_id = self.memory.create_conversation(tenant_id, title=input_text[:80])
        self.memory.add_message(conversation_id, "user", input_text)

        task_id = new_id("task")
        now = utcnow()
        self.db.insert(
            "tasks",
            {
                "id": task_id,
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "status": "running",
                "input": input_text,
                "plan_json": "[]",
                "result": "",
                "trace_json": "[]",
                "created_at": now,
                "updated_at": now,
            },
        )

        trace: List[Dict[str, Any]] = []
        brand = self.brand.get(tenant_id)
        brand_block = brand.prompt_block() if brand else f"Tenant: {tenant_id}"

        plan = await self._plan(input_text, brand_block, trace)
        self.db.update(
            "tasks",
            {"plan_json": dumps(plan), "updated_at": utcnow()},
            "id=?",
            (task_id,),
        )

        step_results: List[Dict[str, Any]] = []
        ctx = ToolContext(tenant_id=tenant_id, task_id=task_id)
        for step in plan.get("steps", []):
            agent_id = step.get("agent", "researcher")
            goal = step.get("goal", input_text)
            result = await self._run_specialist(agent_id, goal, input_text, brand_block, ctx, trace)
            step_results.append({"agent": agent_id, "goal": goal, "result": result})

        final = await self._aggregate(input_text, step_results, brand_block, trace)

        self.db.update(
            "tasks",
            {
                "status": "completed",
                "result": final,
                "trace_json": dumps(trace),
                "updated_at": utcnow(),
            },
            "id=?",
            (task_id,),
        )
        self.memory.add_message(conversation_id, "assistant", final, meta={"task_id": task_id})

        return {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "status": "completed",
            "plan": plan,
            "result": final,
            "trace": trace,
        }

    async def _plan(self, task: str, brand_block: str, trace: List[Dict[str, Any]]) -> Dict[str, Any]:
        planner = self.agents.get("planner")
        system = (planner.system_prompt if planner else "") or (
            "You are the Planner agent. Create a short plan as JSON with keys "
            "`steps` (list of {agent, goal, tools}) and `summary`."
        )
        messages = [
            LLMMessage(role="system", content=f"{system}\n\n{brand_block}"),
            LLMMessage(role="user", content=f"Task: {task}\n\nAvailable agents: {[a.id for a in self.agents.list()]}"),
        ]
        resp = await self.llm.chat(messages)
        parsed = _extract_json(resp.content) or {
            "steps": [
                {"agent": "researcher", "goal": task, "tools": ["kb_search", "clock"]},
                {"agent": "analyst", "goal": "Analyze and recommend", "tools": ["emit_recommendation"]},
            ],
            "summary": "Default two-step plan",
        }
        trace.append(event(LoopPhase.PLAN, model=resp.model, raw=(resp.content or "")[:2000], parsed=parsed))
        return parsed

    async def _run_specialist(
        self,
        agent_id: str,
        goal: str,
        task: str,
        brand_block: str,
        ctx: ToolContext,
        trace: List[Dict[str, Any]],
    ) -> str:
        spec = self.agents.get(agent_id)
        tools_allowed = list((spec.tools if spec else []) or ["kb_search", "clock"])
        schemas = self.tools.openai_tools(tools_allowed)
        skill_bits = "\n\n".join(s.prompt_snippet() for s in self.skills.list()[:5])
        system = (spec.system_prompt if spec else "") or f"You are the {agent_id} specialist."
        system += (
            "\nUse the provided tools via native function calling when helpful. "
            "After tools return, continue until you can give a final textual answer "
            "with no further tool calls."
            f"\nAllowed tools: {tools_allowed}\n\n{brand_block}\n\nSkills:\n{skill_bits}"
        )
        messages: List[LLMMessage] = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=f"Overall task: {task}\nYour goal: {goal}"),
        ]

        answer = ""
        for round_i in range(self.max_tool_rounds):
            resp = await self.llm.chat(messages, tools=schemas or None, tool_choice="auto" if schemas else None)
            tool_calls = list(resp.tool_calls or [])
            legacy_answer: Optional[str] = None

            # Compat fallback: JSON-in-content tool protocol
            if not tool_calls and resp.content:
                legacy_calls, legacy_answer = _legacy_tool_calls_from_content(resp.content)
                if legacy_calls:
                    tool_calls = legacy_calls

            trace.append(
                event(
                    LoopPhase.SPECIALIST,
                    agent=agent_id,
                    round=round_i,
                    model=resp.model,
                    content_preview=(resp.content or "")[:500],
                    tool_call_count=len(tool_calls),
                    tool_call_names=[tc.name for tc in tool_calls],
                )
            )

            if not tool_calls:
                answer = (resp.content or legacy_answer or "").strip()
                if not answer and legacy_answer:
                    answer = legacy_answer
                break

            # Append assistant message with tool_calls (OpenAI wire format)
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=tool_calls,
                )
            )

            for tc in tool_calls:
                allowed = tc.name in tools_allowed or tc.name in {
                    "kb_search",
                    "emit_recommendation",
                    "clock",
                }
                t0 = time.perf_counter()
                if not allowed:
                    ok, output, error = False, None, f"tool not allowed: {tc.name}"
                else:
                    result = await self.tools.invoke(tc.name, tc.arguments or {}, ctx)
                    ok, output, error = result.ok, result.output, result.error
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)

                payload = {
                    "ok": ok,
                    "output": output,
                    "error": error,
                }
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=json.dumps(payload, ensure_ascii=False, default=str),
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )
                trace.append(
                    event(
                        LoopPhase.TOOL,
                        agent=agent_id,
                        round=round_i,
                        id=tc.id,
                        name=tc.name,
                        args_summary=_args_summary(tc.arguments or {}),
                        ok=ok,
                        error=error,
                        latency_ms=latency_ms,
                        output_preview=_args_summary({"output": output}, limit=400) if ok else None,
                    )
                )
        else:
            # Max rounds exhausted — ask once more without tools for a final answer
            resp = await self.llm.chat(
                messages
                + [LLMMessage(role="user", content="Max tool rounds reached. Provide your final answer now.")],
                tools=None,
                tool_choice=None,
            )
            answer = (resp.content or "").strip()
            trace.append(
                event(
                    LoopPhase.SPECIALIST,
                    agent=agent_id,
                    round=self.max_tool_rounds,
                    model=resp.model,
                    note="max_tool_rounds_forced_final",
                    content_preview=answer[:500],
                )
            )

        if not answer:
            answer = f"[{agent_id}] completed {sum(1 for t in trace if t.get("phase") == LoopPhase.TOOL.value and t.get("agent") == agent_id)} tool call(s); no textual final answer."
        return answer

    async def _aggregate(
        self,
        task: str,
        step_results: List[Dict[str, Any]],
        brand_block: str,
        trace: List[Dict[str, Any]],
    ) -> str:
        agg = self.agents.get("aggregator")
        system = (agg.system_prompt if agg else "") or (
            "You are the Aggregator. Synthesize specialist results into a clear final answer."
        )
        messages = [
            LLMMessage(role="system", content=f"{system}\n\n{brand_block}"),
            LLMMessage(
                role="user",
                content=f"Task: {task}\n\nSpecialist results:\n{json.dumps(step_results, ensure_ascii=False)[:6000]}",
            ),
        ]
        resp = await self.llm.chat(messages)
        raw = resp.content or ""
        trace.append(event(LoopPhase.AGGREGATE, model=resp.model, raw=raw[:2000]))
        return raw
