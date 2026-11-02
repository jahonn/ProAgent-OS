"""Task-run HITL pause / resume (mid-loop continue).

Industry alignment (product pauses, not a new chat turn):

* OpenAI Agents — run interruptions + RunState approve/resume
  https://openai.github.io/openai-agents-python/human_in_the_loop/
* Anthropic computer use — confirm consequential actions before they run
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
* LangGraph — interrupt + Command(resume=...) with a checkpointer
  https://docs.langchain.com/oss/python/langgraph/interrupts

This path is **separate** from the proactive recommendation inbox
(``proagent_os.proactive``). Recommendations stay as-is; a task run that needs
confirmation can move to ``awaiting_human`` instead.

``pending_action_json`` is opaque JSON data for humans and auditors. It is
never treated as a URL, shell command, or code to execute (same rule as
recommendation executors).

``hitl_checkpoint_json`` is orchestrator resume state (plan, completed steps,
pause locus). Also opaque — never URL/shell.

**Resume:** when ``PROAGENT_HITL_RESUME_CONTINUE`` is true (default) and a
checkpoint exists, approve re-opens the task to ``running`` so the orchestrator
can inject the human decision and continue remaining plan steps. Without a
checkpoint, or when the flag is false, approve still marks ``completed``
(legacy stub). Reject always cancels; it never continues.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from proagent_os.db import dumps, get_db, loads, utcnow
from proagent_os.harness.loop import LoopPhase, event

logger = logging.getLogger("proagent.hitl")

STATUS_RUNNING = "running"
STATUS_AWAITING_HUMAN = "awaiting_human"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

CHECKPOINT_VERSION = 1

_MAX_REASON_LEN = 2000
_MAX_PAYLOAD_CHARS = 16_000
_MAX_CHECKPOINT_CHARS = 48_000


def sanitize_pending_action(raw: Any) -> Dict[str, Any]:
    """Keep only JSON-serializable object data. Never interpret as URL/shell."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {"_dropped_non_object": True, "type": type(raw).__name__}
    try:
        encoded = json.dumps(raw, ensure_ascii=False, default=str)
    except TypeError:
        return {"_dropped_unserializable": True}
    if len(encoded) > _MAX_PAYLOAD_CHARS:
        return {
            "_truncated": True,
            "preview": encoded[: _MAX_PAYLOAD_CHARS - 32] + "...",
        }
    parsed = json.loads(encoded)
    return parsed if isinstance(parsed, dict) else {"_dropped_non_object": True}


def sanitize_checkpoint(raw: Any) -> Dict[str, Any]:
    """Validate / clip orchestrator checkpoint JSON. Never interpret as code."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {}
    try:
        encoded = json.dumps(raw, ensure_ascii=False, default=str)
    except TypeError:
        return {}
    if len(encoded) > _MAX_CHECKPOINT_CHARS:
        return {"v": CHECKPOINT_VERSION, "_truncated": True}
    parsed = json.loads(encoded)
    return parsed if isinstance(parsed, dict) else {}


def checkpoint_is_resumable(checkpoint: Any) -> bool:
    """True when checkpoint carries enough state to continue after approve."""
    if not isinstance(checkpoint, dict):
        return False
    if checkpoint.get("v") != CHECKPOINT_VERSION:
        return False
    if checkpoint.get("_truncated"):
        return False
    if not checkpoint.get("conversation_id"):
        return False
    if not isinstance(checkpoint.get("input_text"), str):
        return False
    if not isinstance(checkpoint.get("plan"), dict):
        return False
    return True


def _clip_reason(reason: str) -> str:
    text = (reason or "").strip() or "human_approval_required"
    if len(text) > _MAX_REASON_LEN:
        return text[: _MAX_REASON_LEN - 3] + "..."
    return text


class TaskHitlService:
    """Pause / resume / reject helpers for the ``tasks`` row."""

    def __init__(self) -> None:
        self.db = get_db()

    def get_task(self, tenant_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetchone(
            "SELECT * FROM tasks WHERE id=? AND tenant_id=?",
            (task_id, tenant_id),
        )
        if not row:
            return None
        return self._decode(row)

    def pause_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        reason: str,
        pending_action: Optional[Dict[str, Any]] = None,
        actor: str = "",
        trace_extra: Optional[Dict[str, Any]] = None,
        plan: Any = None,
        partial_result: str = "",
        checkpoint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """running → awaiting_human. Stores reason + sanitized pending action."""
        row = self._require(tenant_id, task_id)
        current = row["status"]
        if current == STATUS_AWAITING_HUMAN:
            return row
        if current != STATUS_RUNNING:
            raise ValueError(
                f"cannot pause task in status {current!r}; expected {STATUS_RUNNING!r}"
            )
        reason_s = _clip_reason(reason)
        pending = sanitize_pending_action(pending_action or {})
        ckpt = sanitize_checkpoint(checkpoint or {})
        trace = list(row.get("trace") or [])
        hitl_event = event(
            LoopPhase.HITL,
            action="pause",
            reason=reason_s,
            pending_action_keys=sorted(pending.keys()),
            actor=actor or "",
            has_checkpoint=checkpoint_is_resumable(ckpt),
            **(trace_extra or {}),
        )
        trace.append(hitl_event)
        now = utcnow()
        fields: Dict[str, Any] = {
            "status": STATUS_AWAITING_HUMAN,
            "pause_reason": reason_s,
            "pending_action_json": dumps(pending),
            "hitl_checkpoint_json": dumps(ckpt),
            "hitl_decision": "",
            "hitl_decided_at": "",
            "trace_json": dumps(trace),
            "updated_at": now,
        }
        if plan is not None:
            fields["plan_json"] = dumps(plan)
        if partial_result:
            fields["result"] = partial_result
        self.db.update("tasks", fields, "id=? AND tenant_id=?", (task_id, tenant_id))
        logger.info(
            "task_hitl_pause task_id=%s tenant_id=%s reason=%s checkpoint=%s",
            task_id,
            tenant_id,
            reason_s[:120],
            checkpoint_is_resumable(ckpt),
        )
        return self._require(tenant_id, task_id)

    def resume_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        actor: str = "",
        note: str = "",
        reopen: bool = False,
    ) -> Dict[str, Any]:
        """Approve a paused run.

        * ``reopen=False`` (default for direct service calls): awaiting_human →
          completed (legacy stub; used when mid-loop continue is off or there is
          no checkpoint).
        * ``reopen=True``: awaiting_human → running so the orchestrator can
          continue remaining plan steps with the human decision injected.
        """
        row = self._require(tenant_id, task_id)
        if row["status"] != STATUS_AWAITING_HUMAN:
            raise ValueError(
                f"cannot resume task in status {row['status']!r}; "
                f"expected {STATUS_AWAITING_HUMAN!r}"
            )
        trace = list(row.get("trace") or [])
        if reopen:
            decision_note = (note or "").strip() or "approved; continuing run after HITL"
            next_status = STATUS_RUNNING
            stub = False
        else:
            decision_note = (note or "").strip() or "approved; run marked completed (HITL stub)"
            next_status = STATUS_COMPLETED
            stub = True
        trace.append(
            event(
                LoopPhase.HITL,
                action="resume",
                decision=DECISION_APPROVED,
                actor=actor or "",
                note=decision_note,
                stub=stub,
                reopen=reopen,
            )
        )
        prior = (row.get("result") or "").strip()
        # When reopening, keep partial result for continue; when stub-completing,
        # append the HITL note for auditors.
        if reopen:
            result_text = prior
        else:
            result_text = (
                f"{prior}\n\n[HITL] {decision_note}".strip()
                if prior
                else f"[HITL] {decision_note}"
            )
        now = utcnow()
        fields: Dict[str, Any] = {
            "status": next_status,
            "hitl_decision": DECISION_APPROVED,
            "hitl_decided_at": now,
            "result": result_text,
            "trace_json": dumps(trace),
            "updated_at": now,
        }
        if not reopen:
            # Clear checkpoint once the stub path finishes.
            fields["hitl_checkpoint_json"] = dumps({})
        self.db.update(
            "tasks",
            fields,
            "id=? AND tenant_id=?",
            (task_id, tenant_id),
        )
        logger.info(
            "task_hitl_resume task_id=%s tenant_id=%s actor=%s reopen=%s",
            task_id,
            tenant_id,
            actor or "",
            reopen,
        )
        return self._require(tenant_id, task_id)

    def clear_checkpoint(self, tenant_id: str, task_id: str) -> None:
        """Drop resume checkpoint after a successful continue or cancel."""
        self.db.update(
            "tasks",
            {"hitl_checkpoint_json": dumps({}), "updated_at": utcnow()},
            "id=? AND tenant_id=?",
            (task_id, tenant_id),
        )

    def reject_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        actor: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """awaiting_human → cancelled. Never continues the run."""
        row = self._require(tenant_id, task_id)
        if row["status"] != STATUS_AWAITING_HUMAN:
            raise ValueError(
                f"cannot reject task in status {row['status']!r}; "
                f"expected {STATUS_AWAITING_HUMAN!r}"
            )
        trace = list(row.get("trace") or [])
        decision_note = (note or "").strip() or "rejected; run cancelled"
        trace.append(
            event(
                LoopPhase.HITL,
                action="reject",
                decision=DECISION_REJECTED,
                actor=actor or "",
                note=decision_note,
                stub=True,
            )
        )
        prior = (row.get("result") or "").strip()
        result_text = (
            f"{prior}\n\n[HITL] {decision_note}".strip()
            if prior
            else f"[HITL] {decision_note}"
        )
        now = utcnow()
        self.db.update(
            "tasks",
            {
                "status": STATUS_CANCELLED,
                "hitl_decision": DECISION_REJECTED,
                "hitl_decided_at": now,
                "result": result_text,
                "trace_json": dumps(trace),
                "hitl_checkpoint_json": dumps({}),
                "updated_at": now,
            },
            "id=? AND tenant_id=?",
            (task_id, tenant_id),
        )
        logger.info(
            "task_hitl_reject task_id=%s tenant_id=%s actor=%s",
            task_id,
            tenant_id,
            actor or "",
        )
        return self._require(tenant_id, task_id)

    def _require(self, tenant_id: str, task_id: str) -> Dict[str, Any]:
        row = self.get_task(tenant_id, task_id)
        if not row:
            raise KeyError("task not found")
        return row

    def _decode(self, row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["plan"] = loads(out.pop("plan_json", "[]"), [])
        out["trace"] = loads(out.pop("trace_json", "[]"), [])
        out["pending_action"] = loads(out.pop("pending_action_json", "{}"), {})
        out["hitl_checkpoint"] = loads(out.pop("hitl_checkpoint_json", "{}"), {})
        return out


class RunPaused(Exception):
    """Raised inside the orchestrator when a tool requires human approval."""

    def __init__(
        self,
        *,
        reason: str,
        pending_action: Dict[str, Any],
        agent_id: str = "",
        tool_name: str = "",
        tool_call_id: str = "",
        step_index: int = 0,
        step_goal: str = "",
    ) -> None:
        self.reason = reason
        self.pending_action = pending_action
        self.agent_id = agent_id
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.step_index = step_index
        self.step_goal = step_goal
        super().__init__(reason)
