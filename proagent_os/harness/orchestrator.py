from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from proagent_os.config import get_settings
from proagent_os.db import dumps, get_db, new_id, utcnow
from proagent_os.harness.hitl import (
    CHECKPOINT_VERSION,
    RECEIPT_FAILED,
    RECEIPT_SUCCEEDED,
    HitlExecuteBlocked,
    RunPaused,
    TaskHitlService,
    build_gated_pending_action,
    checkpoint_is_resumable,
    parse_gated_tool_call,
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

logger = logging.getLogger("proagent.orchestrator")

# Tools the specialist loop may call even when the agent YAML omits them.
_ALWAYS_ALLOWED_TOOLS = frozenset(
    {"kb_search", "emit_recommendation", "clock", "request_run_approval"}
)


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


def _json_obj_equal(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """Stable compare for argument snapshots (key order does not matter)."""
    return json.dumps(left, sort_keys=True, ensure_ascii=False, default=str) == json.dumps(
        right, sort_keys=True, ensure_ascii=False, default=str
    )


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
            meta = compaction.meta or {}
            compaction_event: Dict[str, Any] = {
                "conversation_id": conversation_id,
                "dropped_count": compaction.dropped_count,
                "reason": compaction.reason,
                "strategy": meta.get("strategy"),
                "approx_chars_before": meta.get("approx_chars_before"),
                "kept_recent": meta.get("kept_recent"),
            }
            # Present only for the optional LLM summarizer (and its fallback).
            # The extractive default omits these so existing traces stay the same.
            for key in ("summarizer_mode", "summarizer_fallback"):
                if meta.get(key):
                    compaction_event[key] = meta[key]
            trace.append(event(LoopPhase.COMPACTION, **compaction_event))

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
        pending_snapshot = paused.pending_action if isinstance(paused.pending_action, dict) else {}
        gated_arguments = pending_snapshot.get("arguments")
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
        if isinstance(gated_arguments, dict):
            # Same snapshot approve will re-check before ToolRegistry.invoke.
            checkpoint["gated_arguments"] = gated_arguments
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
        reexecute: bool = False,
    ) -> Dict[str, Any]:
        """Approve a paused task; continue mid-loop when checkpoint + flag allow.

        Aligns with OpenAI Agents ``state.approve`` + ``Runner.run(agent, state)``
        and LangGraph ``Command(resume=...)``. Reject stays on ``TaskHitlService``.

        ``reexecute`` is the explicit re-approve for a ``started`` execute
        receipt only. It does not run a ``succeeded`` receipt again.
        """
        token = bind_tenant(tenant_id)
        try:
            return await self._resume_after_hitl(
                tenant_id,
                task_id,
                actor=actor,
                note=note,
                reexecute=reexecute,
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
        reexecute: bool = False,
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
            # Legacy stub: mark completed with decision recorded. Tool is not run.
            return hitl.resume_task(tenant_id, task_id, actor=actor, note=note, reopen=False)

        # Run (or reuse) the gated tool while the task is still awaiting_human.
        # The execute receipt commits before invoke and records the observation
        # before this method re-opens the task. A crash in that window leaves
        # a row a second resume can see, instead of invoking the side effect again.
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        pending = row.get("pending_action") or {}
        if not isinstance(pending, dict):
            pending = {}
        trace = list(row.get("trace") or [])
        decision_note = (note or "").strip() or "approved"
        paused_idx = int(checkpoint.get("paused_step_index") or 0)
        paused_agent = str(checkpoint.get("paused_agent") or "researcher")
        ctx = ToolContext(tenant_id=tenant_id, task_id=task_id)
        base_len = len(trace)
        gated = await self._execute_approved_gated_tool(
            pending=pending,
            checkpoint=checkpoint,
            ctx=ctx,
            trace=trace,
            agent_id=paused_agent,
            execute_enabled=bool(settings.hitl_execute_on_approve),
            decision_note=decision_note,
            reexecute=reexecute,
        )
        spawned = list(trace[base_len:])

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
        paused_goal = str(checkpoint.get("paused_goal") or input_text)
        trace = list(row.get("trace") or [])
        trace.extend(spawned)
        brand = self.brand.get(tenant_id)
        brand_block = brand.prompt_block() if brand else f"Tenant: {tenant_id}"
        prior_block = self.memory.prior_context_block(conversation_id)

        approval_blob = gated["observation"]
        resume_note = gated["note"]
        trace.append(
            event(
                LoopPhase.HITL,
                action="continue",
                decision="approved",
                actor=actor or "",
                note=decision_note,
                step_index=paused_idx,
                tool=gated.get("tool_name") or checkpoint.get("tool_name") or "",
                gated_tool_executed=bool(gated.get("executed")),
                skip_reason=gated.get("skip_reason") or "",
                hitl_receipt=gated.get("receipt") or "",
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
                hitl_seed_tool_name=gated.get("seed_tool_name") or "",
                hitl_seed_tool_call_id=gated.get("seed_tool_call_id") or "",
                hitl_seed_arguments=gated.get("seed_arguments"),
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
            "gated_tool_executed": bool(gated.get("executed")),
            "gated_tool": gated.get("tool_name") or "",
        }

    def _tool_allowed_for_agent(self, agent_id: str, tool_name: str) -> bool:
        spec = self.agents.get(agent_id)
        tools_allowed = list((spec.tools if spec else []) or ["kb_search", "clock"])
        return tool_name in tools_allowed or tool_name in _ALWAYS_ALLOWED_TOOLS

    async def _execute_approved_gated_tool(
        self,
        *,
        pending: Dict[str, Any],
        checkpoint: Dict[str, Any],
        ctx: ToolContext,
        trace: List[Dict[str, Any]],
        agent_id: str,
        execute_enabled: bool,
        decision_note: str,
        reexecute: bool = False,
    ) -> Dict[str, Any]:
        """Run the paused tool once, reuse a stored receipt, or explain a skip.

        Only a ``kind=gated_tool`` snapshot is eligible. The tool must still
        declare ``requires_human_approval``, must match the checkpoint, and
        must pass schema validation again before ``run``. When execution is
        enabled, a receipt is claimed before ``ToolRegistry.invoke`` and the
        observation is stored on that row before this method returns. A
        ``succeeded`` or ``failed`` receipt is reused and not invoked again.
        A ``started`` receipt raises ``HitlExecuteBlocked`` unless
        ``reexecute`` is true. ``proposed`` is never executed. Reject does
        not call this method.
        """
        parsed = parse_gated_tool_call(pending)
        if isinstance(pending.get("proposed"), dict):
            proposed: Dict[str, Any] = pending["proposed"]
        elif parsed is None:
            proposed = pending
        else:
            proposed = {}

        def _note(extra: str) -> str:
            return (
                "A human approved the pending action for this run.\n"
                f"Decision note: {decision_note}\n"
                f"{extra}\n"
                "Pending payload is opaque JSON — never treat it as a URL or shell command: "
                f"{json.dumps(proposed, ensure_ascii=False, default=str)}\n"
                "Do not call the same gated tool again for this action. "
                "Continue toward your goal and finish with a textual answer."
            )

        def _pack(
            *,
            observation: Dict[str, Any],
            note: str,
            tool_name: str = "",
            tool_call_id: str = "",
            arguments: Optional[Dict[str, Any]] = None,
            executed_flag: bool = False,
            skip_reason: str = "",
            seed: bool = True,
            receipt: str = "",
        ) -> Dict[str, Any]:
            seed_args = arguments if isinstance(arguments, dict) else None
            return {
                "observation": observation,
                "note": note,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": seed_args if seed_args is not None else {},
                "executed": executed_flag,
                "skip_reason": skip_reason,
                "receipt": receipt,
                "seed_tool_name": tool_name if seed and tool_name else "",
                "seed_tool_call_id": tool_call_id if seed and tool_call_id else "",
                "seed_arguments": seed_args,
            }

        def _ack(
            tool_name: str,
            tool_call_id: str,
            *,
            error: Optional[str] = None,
            output_extra: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            output: Dict[str, Any] = {
                "needs_human_approval": False,
                "approved": True,
                "decision": "approved",
                "note": decision_note,
                "action": proposed,
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "executed": False,
            }
            if output_extra:
                output.update(output_extra)
            return {"ok": error is None, "output": output, "error": error}

        if parsed is None:
            packed = _pack(
                observation=_ack(
                    str(checkpoint.get("tool_name") or ""),
                    str(checkpoint.get("tool_call_id") or ""),
                ),
                note=_note(
                    "No structured gated tool was stored. The run continues without executing a tool."
                ),
                tool_name=str(checkpoint.get("tool_name") or ""),
                tool_call_id=str(checkpoint.get("tool_call_id") or ""),
                executed_flag=False,
                skip_reason="no_gated_tool",
                seed=False,
            )
            logger.info(
                "hitl_execute_skip task_id=%s reason=no_gated_tool",
                ctx.task_id or "",
            )
            return packed

        tool_name = parsed["tool"]
        tool_call_id = parsed["tool_call_id"]
        arguments = parsed["arguments"]

        def _skip(reason: str, extra: str, *, error: str) -> Dict[str, Any]:
            logger.info(
                "hitl_execute_skip task_id=%s tool=%s reason=%s",
                ctx.task_id or "",
                tool_name,
                reason,
            )
            return _pack(
                observation=_ack(
                    tool_name,
                    tool_call_id,
                    error=error,
                    output_extra={"refused": True},
                ),
                note=_note(extra),
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments,
                executed_flag=False,
                skip_reason=reason,
            )

        if not execute_enabled:
            logger.info(
                "hitl_execute_skip task_id=%s tool=%s reason=flag_off",
                ctx.task_id or "",
                tool_name,
            )
            return _pack(
                observation=_ack(tool_name, tool_call_id),
                note=_note(
                    "PROAGENT_HITL_EXECUTE_ON_APPROVE is off. The gated tool was not executed."
                ),
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments,
                executed_flag=False,
                skip_reason="flag_off",
            )

        ckpt_tool = checkpoint.get("tool_name")
        if not isinstance(ckpt_tool, str) or ckpt_tool != tool_name:
            return _skip(
                "tool_mismatch",
                "Gated tool name did not match the checkpoint. The tool was not executed.",
                error="gated tool does not match checkpoint",
            )

        ckpt_args = checkpoint.get("gated_arguments")
        if not isinstance(ckpt_args, dict) or not _json_obj_equal(ckpt_args, arguments):
            return _skip(
                "arguments_mismatch",
                "Gated argument snapshot did not match the checkpoint. The tool was not executed.",
                error="gated arguments do not match checkpoint",
            )

        tool_obj = self.tools.get(tool_name)
        if tool_obj is None:
            return _skip(
                "unknown_tool",
                "The gated tool is not registered. It was not executed.",
                error=f"Unknown tool: {tool_name}",
            )
        if not getattr(tool_obj, "requires_human_approval", False):
            return _skip(
                "not_approval_gated",
                "The pending tool is not marked requires_human_approval. "
                "It was not executed. Free-text actions are never run as a URL or shell command.",
                error="refusing to execute a tool that does not require human approval",
            )
        if not self._tool_allowed_for_agent(agent_id, tool_name):
            return _skip(
                "tool_not_allowed",
                "The gated tool is not allowed for this specialist. It was not executed.",
                error=f"tool not allowed: {tool_name}",
            )

        # Schema before the claim so a bad snapshot does not take the exactly-once slot.
        schema_check = self.tools.validate(tool_name, arguments)
        if not schema_check.ok:
            logger.info(
                "hitl_execute_skip task_id=%s tool=%s reason=schema_validation_failed",
                ctx.task_id or "",
                tool_name,
            )
            trace.append(
                event(
                    LoopPhase.TOOL,
                    agent=agent_id,
                    id=tool_call_id,
                    name=tool_name,
                    args_summary=_args_summary(arguments),
                    ok=False,
                    error=schema_check.error,
                    latency_ms=0,
                    hitl_executed=False,
                    schema_validation_failed=True,
                    output_preview=None,
                )
            )
            return _pack(
                observation={
                    "ok": False,
                    "output": schema_check.output,
                    "error": schema_check.error,
                },
                note=_note("The gated tool failed schema validation and was not executed."),
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments,
                executed_flag=False,
                skip_reason="schema_validation_failed",
            )

        if not ctx.task_id:
            raise HitlExecuteBlocked(
                "Missing task id. Refusing to run the gated tool without an execute receipt."
            )

        hitl_receipts = TaskHitlService()
        decision = hitl_receipts.resolve_execute_receipt(
            tenant_id=ctx.tenant_id,
            task_id=ctx.task_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            reexecute=reexecute,
        )
        receipt_row = decision["receipt"]
        receipt_id = str(receipt_row.get("id") or "")

        def _already_traced() -> bool:
            return any(
                ev.get("phase") == LoopPhase.TOOL.value
                and ev.get("id") == tool_call_id
                and ev.get("hitl_executed") is True
                for ev in trace
            )

        if decision["action"] == "reuse":
            observation = decision["observation"]
            if not isinstance(observation, dict):
                observation = {"ok": False, "output": None, "error": "missing observation"}
            schema_failed = (
                isinstance(observation.get("output"), dict)
                and observation["output"].get("error") == "schema_validation_failed"
            )
            stored_ok = bool(observation.get("ok")) and not schema_failed
            # A failed receipt means invoke already happened (unless schema failed).
            if schema_failed:
                ran = False
                skip = "schema_validation_failed"
                extra = "The gated tool failed schema validation and was not executed."
            elif stored_ok:
                ran = True
                skip = ""
                extra = (
                    f"The gated tool `{tool_name}` already ran once. "
                    "This resume reused the stored observation and did not invoke it again."
                )
            else:
                ran = True
                skip = "tool_error"
                extra = (
                    f"The gated tool `{tool_name}` was already invoked and returned an error. "
                    "This resume reused the stored observation and did not invoke it again."
                )
            if not _already_traced():
                trace.append(
                    event(
                        LoopPhase.TOOL,
                        agent=agent_id,
                        id=tool_call_id,
                        name=tool_name,
                        args_summary=_args_summary(arguments),
                        ok=bool(observation.get("ok")),
                        error=observation.get("error"),
                        latency_ms=0,
                        hitl_executed=ran,
                        hitl_receipt="reused",
                        hitl_receipt_id=receipt_id,
                        schema_validation_failed=schema_failed,
                        output_preview=(
                            _args_summary({"output": observation.get("output")}, limit=400)
                            if observation.get("ok")
                            else None
                        ),
                    )
                )
            logger.info(
                "hitl_execute_reuse task_id=%s tool=%s receipt_id=%s status=%s",
                ctx.task_id,
                tool_name,
                receipt_id,
                receipt_row.get("status"),
            )
            return _pack(
                observation=observation,
                note=_note(extra),
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments,
                executed_flag=ran,
                skip_reason=skip,
                receipt="reused",
            )

        t0 = time.perf_counter()
        result = await self.tools.invoke(tool_name, arguments, ctx)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        schema_failed = (
            isinstance(result.output, dict) and result.output.get("error") == "schema_validation_failed"
        )
        observation = {"ok": result.ok, "output": result.output, "error": result.error}
        # Finish the receipt before returning. A crash after invoke and before
        # this update leaves status=started, which a later resume will not re-run.
        if schema_failed or not result.ok:
            finish_status = RECEIPT_FAILED
        else:
            finish_status = RECEIPT_SUCCEEDED
        hitl_receipts.finish_execute_receipt(
            receipt_id,
            status=finish_status,
            observation=observation,
            error=result.error or "",
        )
        ran = not schema_failed
        if not _already_traced():
            trace.append(
                event(
                    LoopPhase.TOOL,
                    agent=agent_id,
                    id=tool_call_id,
                    name=tool_name,
                    args_summary=_args_summary(arguments),
                    ok=result.ok,
                    error=result.error,
                    latency_ms=latency_ms,
                    hitl_executed=ran,
                    hitl_receipt="recorded",
                    hitl_receipt_id=receipt_id,
                    schema_validation_failed=schema_failed,
                    output_preview=_args_summary({"output": result.output}, limit=400) if result.ok else None,
                )
            )
        if schema_failed:
            extra = "The gated tool failed schema validation and was not executed."
            skip = "schema_validation_failed"
        elif result.ok:
            extra = f"The gated tool `{tool_name}` ran once through the tool registry."
            skip = ""
        else:
            extra = f"The gated tool `{tool_name}` was invoked and returned an error."
            skip = "tool_error"
        logger.info(
            "hitl_execute task_id=%s tool=%s ok=%s schema_failed=%s receipt_id=%s",
            ctx.task_id or "",
            tool_name,
            result.ok,
            schema_failed,
            receipt_id,
        )
        return _pack(
            observation=observation,
            note=_note(extra),
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            executed_flag=ran,
            skip_reason=skip,
            receipt="recorded",
        )

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
        hitl_seed_tool_name: str = "",
        hitl_seed_tool_call_id: str = "",
        hitl_seed_arguments: Optional[Dict[str, Any]] = None,
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
            seed_name = hitl_seed_tool_name or "request_run_approval"
            seed_id = hitl_seed_tool_call_id or "hitl_resume_seed"
            if isinstance(hitl_seed_arguments, dict):
                seed_args = hitl_seed_arguments
            else:
                seed_args = {"reason": "previously_paused", "action": {}}
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id=seed_id,
                            name=seed_name,
                            arguments=seed_args,
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
                    tool_call_id=seed_id,
                    name=seed_name,
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
                allowed = self._tool_allowed_for_agent(agent_id, tc.name)
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
                    proposed = args.get("action") if isinstance(args.get("action"), dict) else {}
                    pending = build_gated_pending_action(
                        tool_name=tc.name,
                        tool_call_id=tc.id or "",
                        arguments=args,
                        proposed=proposed,
                    )
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
