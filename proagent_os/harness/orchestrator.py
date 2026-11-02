from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from proagent_os.config import get_settings
from proagent_os.db import dumps, get_db, new_id, utcnow
from proagent_os.harness.hitl import (
    CHECKPOINT_VERSION,
    RunPaused,
    TaskHitlService,
    checkpoint_is_resumable,
    sanitize_pending_action,
)
from proagent_os.harness.loop import LoopPhase, event
from proagent_os.harness.memory import ConversationMemory
from proagent_os.harness.registry import AgentRegistry
from proagent_os.harness.replan import observations_summary, should_replan
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

    Optional outer **plan-and-replan** ring (``PROAGENT_REPLAN_*``, default off):
    after specialist steps, heuristic triggers may re-call the planner with the
    prior plan + observations (capped by ``replan_max``).
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

        # Session compaction stub: fold older turns when over thresholds.
        # Originals stay in messages; summary row + tenant_memory are auditable.
        compaction = self.memory.compact_if_needed(
            conversation_id, tenant_id=tenant_id, persist=True
        )
        prior_block = self.memory.prior_context_block(conversation_id)

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

        if compaction.compacted:
            trace.append(
                event(
                    LoopPhase.COMPACTION,
                    conversation_id=conversation_id,
                    dropped_count=compaction.dropped_count,
                    reason=compaction.reason,
                    strategy=(compaction.meta or {}).get("strategy"),
                    approx_chars_before=(compaction.meta or {}).get("approx_chars_before"),
                    kept_recent=(compaction.meta or {}).get("kept_recent"),
                )
            )

        settings = get_settings()
        plan = await self._plan(input_text, brand_block, trace, prior_context=prior_block)
        self.db.update(
            "tasks",
            {"plan_json": dumps(plan), "updated_at": utcnow()},
            "id=?",
            (task_id,),
        )

        step_results: List[Dict[str, Any]] = []
        ctx = ToolContext(tenant_id=tenant_id, task_id=task_id)
        hitl = TaskHitlService()
        replan_attempt = 0
        while True:
            step_results = []
            try:
                for step_index, step in enumerate(plan.get("steps", [])):
                    agent_id = step.get("agent", "researcher")
                    goal = step.get("goal", input_text)
                    result = await self._run_specialist(
                        agent_id,
                        goal,
                        input_text,
                        brand_block,
                        ctx,
                        trace,
                        prior_context=prior_block,
                        step_index=step_index,
                    )
                    step_results.append({"agent": agent_id, "goal": goal, "result": result})
            except RunPaused as paused:
                return self._pause_for_hitl(
                    hitl=hitl,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    input_text=input_text,
                    plan=plan,
                    step_results=step_results,
                    trace=trace,
                    paused=paused,
                )

            do_replan, reason = should_replan(
                plan,
                step_results,
                enabled=settings.replan_enabled,
                attempt=replan_attempt,
                max_replans=settings.replan_max,
                force=settings.replan_force,
            )
            if not do_replan:
                break

            obs = observations_summary(plan, step_results)
            next_attempt = replan_attempt + 1
            trace.append(
                event(
                    LoopPhase.REPLAN,
                    attempt=next_attempt,
                    reason=reason,
                    prior_steps=len(plan.get("steps") or []),
                    prior_step_results=len(step_results),
                    observations_preview=obs[:500],
                )
            )
            plan = await self._plan(
                input_text,
                brand_block,
                trace,
                prior_context=prior_block,
                phase=LoopPhase.REPLAN,
                prior_plan=plan,
                observations=obs,
            )
            replan_attempt = next_attempt
            self.db.update(
                "tasks",
                {"plan_json": dumps(plan), "updated_at": utcnow()},
                "id=?",
                (task_id,),
            )

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

    def _pause_for_hitl(
        self,
        *,
        hitl: TaskHitlService,
        tenant_id: str,
        task_id: str,
        conversation_id: str,
        input_text: str,
        plan: Dict[str, Any],
        step_results: List[Dict[str, Any]],
        trace: List[Dict[str, Any]],
        paused: RunPaused,
    ) -> Dict[str, Any]:
        """Persist checkpoint + pause. HITL short-circuits replan."""
        partial = ""
        if step_results:
            partial = json.dumps(step_results, ensure_ascii=False, default=str)[:4000]
        checkpoint = {
            "v": CHECKPOINT_VERSION,
            "conversation_id": conversation_id,
            "input_text": input_text,
            "plan": plan,
            "completed_steps": step_results,
            "paused_step_index": paused.step_index,
            "paused_agent": paused.agent_id,
            "paused_goal": paused.step_goal or "",
            "tool_name": paused.tool_name,
            "tool_call_id": paused.tool_call_id,
            "pause_reason": paused.reason,
        }
        self.db.update(
            "tasks",
            {
                "plan_json": dumps(plan),
                "trace_json": dumps(trace),
                "result": partial,
                "updated_at": utcnow(),
            },
            "id=?",
            (task_id,),
        )
        paused_row = hitl.pause_task(
            tenant_id,
            task_id,
            reason=paused.reason,
            pending_action=paused.pending_action,
            actor="orchestrator",
            trace_extra={
                "agent": paused.agent_id,
                "tool": paused.tool_name,
                "tool_call_id": paused.tool_call_id,
                "step_index": paused.step_index,
            },
            plan=plan,
            partial_result=partial,
            checkpoint=checkpoint,
        )
        self.memory.add_message(
            conversation_id,
            "assistant",
            f"[awaiting_human] {paused.reason}",
            meta={"task_id": task_id, "status": "awaiting_human"},
        )
        return {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "status": paused_row["status"],
            "plan": plan,
            "result": paused_row.get("result") or "",
            "trace": paused_row.get("trace") or [],
            "pause_reason": paused_row.get("pause_reason") or paused.reason,
            "pending_action": paused_row.get("pending_action") or {},
            "hitl_checkpoint": paused_row.get("hitl_checkpoint") or {},
            "hitl": "pause",
        }

    async def resume_after_hitl(
        self,
        tenant_id: str,
        task_id: str,
        *,
        actor: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """Approve a paused task; continue mid-loop when checkpoint + flag allow.

        Aligns with OpenAI Agents ``state.approve`` + ``Runner.run(agent, state)``
        and LangGraph ``Command(resume=...)``. Reject stays on ``TaskHitlService``.
        """
        token = bind_tenant(tenant_id)
        try:
            return await self._resume_after_hitl(
                tenant_id, task_id, actor=actor, note=note
            )
        finally:
            unbind_tenant(token)

    async def _resume_after_hitl(
        self,
        tenant_id: str,
        task_id: str,
        *,
        actor: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        hitl = TaskHitlService()
        row = hitl.get_task(tenant_id, task_id)
        if not row:
            raise KeyError("task not found")
        if row["status"] != "awaiting_human":
            raise ValueError(
                f"cannot resume task in status {row['status']!r}; expected 'awaiting_human'"
            )

        settings = get_settings()
        checkpoint = row.get("hitl_checkpoint") or {}
        can_continue = bool(settings.hitl_resume_continue) and checkpoint_is_resumable(
            checkpoint
        )
        if not can_continue:
            # Legacy stub: mark completed with decision recorded.
            return hitl.resume_task(tenant_id, task_id, actor=actor, note=note, reopen=False)

        # Re-open to running and continue remaining plan steps.
        row = hitl.resume_task(tenant_id, task_id, actor=actor, note=note, reopen=True)
        conversation_id = str(checkpoint["conversation_id"])
        input_text = str(checkpoint["input_text"])
        plan = checkpoint.get("plan") if isinstance(checkpoint.get("plan"), dict) else (row.get("plan") or {})
        if not isinstance(plan, dict):
            plan = {"steps": [], "summary": ""}
        step_results: List[Dict[str, Any]] = list(checkpoint.get("completed_steps") or [])
        if not isinstance(step_results, list):
            step_results = []
        paused_idx = int(checkpoint.get("paused_step_index") or 0)
        paused_agent = str(checkpoint.get("paused_agent") or "researcher")
        paused_goal = str(checkpoint.get("paused_goal") or input_text)
        pending = row.get("pending_action") or {}
        trace: List[Dict[str, Any]] = list(row.get("trace") or [])
        brand = self.brand.get(tenant_id)
        brand_block = brand.prompt_block() if brand else f"Tenant: {tenant_id}"
        prior_block = self.memory.prior_context_block(conversation_id)
        ctx = ToolContext(tenant_id=tenant_id, task_id=task_id)

        decision_note = (note or "").strip() or "approved"
        approval_blob = {
            "ok": True,
            "output": {
                "needs_human_approval": False,
                "approved": True,
                "decision": "approved",
                "note": decision_note,
                "action": pending,
                "tool": checkpoint.get("tool_name") or "",
                "tool_call_id": checkpoint.get("tool_call_id") or "",
                "executed": False,
            },
            "error": None,
        }
        resume_note = (
            "A human approved the pending action for this run.\n"
            f"Decision note: {decision_note}\n"
            "Pending action (opaque JSON — never treat as URL/shell): "
            f"{json.dumps(pending, ensure_ascii=False, default=str)}\n"
            "Do not call request_run_approval again for the same action. "
            "Continue toward your goal and finish with a textual answer."
        )
        trace.append(
            event(
                LoopPhase.HITL,
                action="continue",
                decision="approved",
                actor=actor or "",
                note=decision_note,
                step_index=paused_idx,
                tool=checkpoint.get("tool_name") or "",
            )
        )

        try:
            # Finish the paused specialist with the human decision injected.
            result = await self._run_specialist(
                paused_agent,
                paused_goal,
                input_text,
                brand_block,
                ctx,
                trace,
                prior_context=prior_block,
                step_index=paused_idx,
                hitl_resume_note=resume_note,
                hitl_approval_observation=approval_blob,
            )
            step_results.append(
                {
                    "agent": paused_agent,
                    "goal": paused_goal,
                    "result": result,
                    "hitl_resumed": True,
                }
            )

            steps = list(plan.get("steps") or [])
            for step_index, step in enumerate(steps):
                if step_index <= paused_idx:
                    continue
                agent_id = step.get("agent", "researcher")
                goal = step.get("goal", input_text)
                step_out = await self._run_specialist(
                    agent_id,
                    goal,
                    input_text,
                    brand_block,
                    ctx,
                    trace,
                    prior_context=prior_block,
                    step_index=step_index,
                )
                step_results.append({"agent": agent_id, "goal": goal, "result": step_out})
        except RunPaused as paused:
            # Nested HITL during continue — pause again with a fresh checkpoint.
            return self._pause_for_hitl(
                hitl=hitl,
                tenant_id=tenant_id,
                task_id=task_id,
                conversation_id=conversation_id,
                input_text=input_text,
                plan=plan,
                step_results=step_results,
                trace=trace,
                paused=paused,
            )

        final = await self._aggregate(input_text, step_results, brand_block, trace)
        now = utcnow()
        self.db.update(
            "tasks",
            {
                "status": "completed",
                "result": final,
                "plan_json": dumps(plan),
                "trace_json": dumps(trace),
                "hitl_checkpoint_json": dumps({}),
                "updated_at": now,
            },
            "id=?",
            (task_id,),
        )
        self.memory.add_message(
            conversation_id,
            "assistant",
            final,
            meta={"task_id": task_id, "status": "completed", "hitl_resumed": True},
        )
        return {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "status": "completed",
            "plan": plan,
            "result": final,
            "trace": trace,
            "hitl_decision": "approved",
            "hitl": "continued",
            "pending_action": pending,
        }

    async def _plan(
        self,
        task: str,
        brand_block: str,
        trace: List[Dict[str, Any]],
        *,
        prior_context: str = "",
        phase: LoopPhase = LoopPhase.PLAN,
        prior_plan: Optional[Dict[str, Any]] = None,
        observations: str = "",
    ) -> Dict[str, Any]:
        planner = self.agents.get("planner")
        system = (planner.system_prompt if planner else "") or (
            "You are the Planner agent. Create a short plan as JSON with keys "
            "`steps` (list of {agent, goal, tools}) and `summary`."
        )
        if phase == LoopPhase.REPLAN or prior_plan is not None:
            system += (
                " Revise the plan given the prior plan and observations. "
                "Return JSON with the same `steps` / `summary` shape."
            )
        user_content = f"Task: {task}\n\nAvailable agents: {[a.id for a in self.agents.list()]}"
        if prior_plan is not None:
            user_content += (
                f"\n\nPrior plan:\n{json.dumps(prior_plan, ensure_ascii=False, default=str)[:3000]}"
            )
        if observations:
            user_content += f"\n\nObservations from executed steps:\n{observations[:4000]}"
        if prior_context:
            user_content = (
                f"Prior conversation (compacted):\n{prior_context}\n\n{user_content}"
            )
        messages = [
            LLMMessage(role="system", content=f"{system}\n\n{brand_block}"),
            LLMMessage(role="user", content=user_content),
        ]
        resp = await self.llm.chat(messages)
        parsed = _extract_json(resp.content) or {
            "steps": [
                {"agent": "researcher", "goal": task, "tools": ["kb_search", "clock"]},
                {"agent": "analyst", "goal": "Analyze and recommend", "tools": ["emit_recommendation"]},
            ],
            "summary": "Default two-step plan",
        }
        trace.append(
            event(
                phase,
                model=resp.model,
                raw=(resp.content or "")[:2000],
                parsed=parsed,
                replan=(phase == LoopPhase.REPLAN),
            )
        )
        return parsed

    async def _run_specialist(
        self,
        agent_id: str,
        goal: str,
        task: str,
        brand_block: str,
        ctx: ToolContext,
        trace: List[Dict[str, Any]],
        *,
        prior_context: str = "",
        step_index: int = 0,
        hitl_resume_note: str = "",
        hitl_approval_observation: Optional[Dict[str, Any]] = None,
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
        user_blob = f"Overall task: {task}\nYour goal: {goal}"
        if prior_context:
            user_blob = (
                f"Prior conversation (compacted):\n{prior_context}\n\n{user_blob}"
            )
        messages: List[LLMMessage] = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=user_blob),
        ]
        if hitl_resume_note:
            messages.append(LLMMessage(role="user", content=hitl_resume_note))
        if hitl_approval_observation is not None:
            # Seed the approved tool observation so the model continues mid-loop
            # without re-requesting the same approval (OpenAI-style resume).
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="hitl_resume_seed",
                            name="request_run_approval",
                            arguments={"reason": "previously_paused", "action": {}},
                            raw_arguments="",
                        )
                    ],
                )
            )
            messages.append(
                LLMMessage(
                    role="tool",
                    content=json.dumps(
                        hitl_approval_observation, ensure_ascii=False, default=str
                    ),
                    tool_call_id="hitl_resume_seed",
                    name="request_run_approval",
                )
            )

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
                    "request_run_approval",
                }
                t0 = time.perf_counter()
                tool_obj = self.tools.get(tc.name)
                if (
                    allowed
                    and tool_obj is not None
                    and getattr(tool_obj, "requires_human_approval", False)
                ):
                    args = tc.arguments if isinstance(tc.arguments, dict) else {}
                    # Schema-harden before pause so bad args never become pending_action.
                    schema_check = self.tools.validate(tc.name, args)
                    if not schema_check.ok:
                        ok, output, error = False, schema_check.output, schema_check.error
                        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                        payload = {"ok": ok, "output": output, "error": error}
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
                                args_summary=_args_summary(args),
                                ok=ok,
                                error=error,
                                latency_ms=latency_ms,
                                schema_validation_failed=True,
                                output_preview=None,
                            )
                        )
                        continue
                    reason = str(args.get("reason") or f"approval required for tool {tc.name}")
                    pending = sanitize_pending_action(args.get("action") if "action" in args else args)
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    trace.append(
                        event(
                            LoopPhase.TOOL,
                            agent=agent_id,
                            round=round_i,
                            id=tc.id,
                            name=tc.name,
                            args_summary=_args_summary(args),
                            ok=True,
                            error=None,
                            latency_ms=latency_ms,
                            hitl_pause=True,
                            output_preview="paused_for_human",
                        )
                    )
                    raise RunPaused(
                        reason=reason,
                        pending_action=pending,
                        agent_id=agent_id,
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        step_index=step_index,
                        step_goal=goal,
                    )
                if not allowed:
                    ok, output, error = False, None, f"tool not allowed: {tc.name}"
                else:
                    result = await self.tools.invoke(tc.name, tc.arguments or {}, ctx)
                    ok, output, error = result.ok, result.output, result.error
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                schema_failed = (
                    isinstance(output, dict) and output.get("error") == "schema_validation_failed"
                )

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
                        schema_validation_failed=schema_failed,
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
            tool_count = sum(
                1
                for t in trace
                if t.get("phase") == LoopPhase.TOOL.value and t.get("agent") == agent_id
            )
            answer = (
                f"[{agent_id}] completed {tool_count} tool call(s); "
                "no textual final answer."
            )
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
