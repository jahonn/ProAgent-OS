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
recommendation executors). When a tool with ``requires_human_approval`` pauses
the run, the payload is a structured snapshot (``kind=gated_tool``, tool name,
validated arguments). Approve may re-invoke that tool through ``ToolRegistry``
only — never by interpreting free text as a URL or shell command.

``hitl_checkpoint_json`` is orchestrator resume state (plan, completed steps,
pause locus, argument snapshot). Also opaque — never URL/shell.

**Resume:** when ``PROAGENT_HITL_RESUME_CONTINUE`` is true (default) and a
checkpoint exists, approve re-opens the task to ``running``. If
``PROAGENT_HITL_EXECUTE_ON_APPROVE`` is also true (default) and the pending
payload is a gated-tool snapshot, the orchestrator runs that tool once
(schema checked again) and injects the observation, then continues remaining
plan steps. That execute path is guarded by ``hitl_execute_receipts``
(migration ``0007``): a ``started`` row is committed before ``invoke``, and
the same row is updated to ``succeeded`` or ``failed`` with an observation
snippet before the task is treated as done. A later resume reuses a
``succeeded`` or ``failed`` observation and does not invoke again. A
``started`` row (crash mid-flight) fails closed: no automatic re-run;
``reexecute=true`` on resume is the explicit re-approve, and it may duplicate
a side effect. A ``succeeded`` receipt is never run again. Execute flag false:
continue with an ack and do not run the tool. Without a checkpoint, or when
the continue flag is false, approve still marks ``completed`` (legacy stub)
and does not run the tool. Reject always cancels; it never continues and
never executes the gated tool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional

from proagent_os.db import dumps, get_db, loads, new_id, utcnow
from proagent_os.harness.loop import LoopPhase, event

logger = logging.getLogger("proagent.hitl")

STATUS_RUNNING = "running"
STATUS_AWAITING_HUMAN = "awaiting_human"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

CHECKPOINT_VERSION = 1

# Structured pending_action written when a requires_human_approval tool pauses.
# ``arguments`` is the validated snapshot. It is not a shell string or a URL.
GATED_TOOL_KIND = "gated_tool"
GATED_PENDING_VERSION = 1
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_MAX_REASON_LEN = 2000
_MAX_PAYLOAD_CHARS = 16_000
_MAX_CHECKPOINT_CHARS = 48_000
_MAX_OBSERVATION_CHARS = 8_000
_MAX_RECEIPT_ERROR_CHARS = 500

# hitl_execute_receipts.status. ``started`` is claimed before invoke.
# ``succeeded`` / ``failed`` mean the outcome was stored. Do not add a status
# that means "please retry the side effect automatically".
RECEIPT_STARTED = "started"
RECEIPT_SUCCEEDED = "succeeded"
RECEIPT_FAILED = "failed"


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


def build_gated_pending_action(
    *,
    tool_name: str,
    tool_call_id: str,
    arguments: Dict[str, Any],
    proposed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Snapshot a paused tool call so approve can re-invoke that same tool.

    ``arguments`` must already have passed schema validation. ``proposed`` is
    optional human-facing context (often ``arguments["action"]``). Callers
    execute only ``arguments``, and only through ToolRegistry — never
    ``proposed``, and never as a URL or shell command.
    """
    args = arguments if isinstance(arguments, dict) else {}
    proposed_obj = proposed if isinstance(proposed, dict) else {}
    name = tool_name if isinstance(tool_name, str) else ""
    call_id = tool_call_id if isinstance(tool_call_id, str) else ""
    return sanitize_pending_action(
        {
            "v": GATED_PENDING_VERSION,
            "kind": GATED_TOOL_KIND,
            "tool": name,
            "tool_call_id": call_id,
            "arguments": args,
            "proposed": proposed_obj,
        }
    )


def parse_gated_tool_call(pending: Any) -> Optional[Dict[str, Any]]:
    """Return ``{tool, tool_call_id, arguments}`` for a gated snapshot.

    Returns None for opaque human JSON, non-objects, URLs, and shell-like
    names. This function does not execute anything.
    """
    if not isinstance(pending, dict):
        return None
    if pending.get("_truncated") or pending.get("_dropped_non_object"):
        return None
    if pending.get("kind") != GATED_TOOL_KIND:
        return None
    if pending.get("v") != GATED_PENDING_VERSION:
        return None
    tool = pending.get("tool")
    if not isinstance(tool, str) or _TOOL_NAME_RE.match(tool) is None:
        return None
    args = pending.get("arguments")
    if not isinstance(args, dict):
        return None
    call_id = pending.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = "hitl_approved"
    else:
        call_id = call_id.strip()[:128]
    return {"tool": tool, "tool_call_id": call_id, "arguments": args}


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


class HitlExecuteBlocked(Exception):
    """Fail-closed: a gated tool has an in-flight execute receipt.

    The side effect might already have run. Callers must not invoke again
    unless the operator explicitly re-approves (``reexecute=true``).
    """

    def __init__(self, message: str, *, receipt_id: str = "") -> None:
        super().__init__(message)
        self.receipt_id = receipt_id


def execute_fingerprint(task_id: str, tool_name: str, arguments: Dict[str, Any]) -> tuple[str, str]:
    """Return ``(args_hash, fingerprint)`` for one gated execute.

    The fingerprint is ``sha256`` of canonical JSON ``{task_id, tool, args_hash}``.
    ``args_hash`` is ``sha256`` of the arguments with sorted keys. Raw arguments
    are not part of the receipt identity. Key order does not change the hash.
    """
    canonical_args = json.dumps(
        arguments if isinstance(arguments, dict) else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    args_hash = hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()
    material = json.dumps(
        {"args_hash": args_hash, "task_id": task_id, "tool": tool_name},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return args_hash, fingerprint


def observation_snippet(payload: Dict[str, Any]) -> str:
    """JSON observation stored on the receipt. Clipped so one row stays small.

    The snippet is what a later resume re-injects. It is data, not a URL or
    a shell command.
    """
    encoded = dumps(payload)
    if len(encoded) <= _MAX_OBSERVATION_CHARS:
        return encoded
    error = payload.get("error")
    error_text = error if isinstance(error, str) else ""
    if len(error_text) > 500:
        error_text = error_text[:497] + "..."
    preview = dumps(payload.get("output"))
    clipped: Dict[str, Any] = {
        "ok": bool(payload.get("ok")),
        "error": error_text or None,
        "output": {"_truncated": True, "preview": preview[:3500]},
        "observation_truncated": True,
    }
    encoded = dumps(clipped)
    if len(encoded) <= _MAX_OBSERVATION_CHARS:
        return encoded
    clipped["output"] = {"_truncated": True, "preview": preview[:1000]}
    return dumps(clipped)


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

    def get_execute_receipt(self, tenant_id: str, fingerprint: str) -> Optional[Dict[str, Any]]:
        """Return the receipt for this fingerprint, scoped to the tenant.

        A fingerprint that exists for another tenant fails closed. That row is
        not returned and the caller must not invoke.
        """
        row = self.db.fetchone(
            "SELECT * FROM hitl_execute_receipts WHERE fingerprint=?",
            (fingerprint,),
        )
        if not row:
            return None
        if row["tenant_id"] != tenant_id:
            raise HitlExecuteBlocked(
                "Execute receipt tenant does not match this task. Refusing to run the gated tool.",
                receipt_id=str(row.get("id") or ""),
            )
        return row

    def list_execute_receipts(self, tenant_id: str, task_id: str) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            "SELECT * FROM hitl_execute_receipts WHERE tenant_id=? AND task_id=? "
            "ORDER BY created_at",
            (tenant_id, task_id),
        )

    def claim_execute_receipt(
        self,
        *,
        tenant_id: str,
        task_id: str,
        tool_call_id: str,
        tool_name: str,
        args_hash: str,
        fingerprint: str,
    ) -> tuple[Dict[str, Any], bool]:
        """Insert ``started`` or return the row that already owns ``fingerprint``.

        The bool is True only when this call inserted the row. Callers invoke
        only when they created it, or when the operator explicitly re-approves
        an existing ``started`` row. The insert commits before ``invoke``.
        """
        existing = self.get_execute_receipt(tenant_id, fingerprint)
        if existing is not None:
            return existing, False
        now = utcnow()
        receipt_id = new_id("rx")
        try:
            self.db.insert(
                "hitl_execute_receipts",
                {
                    "id": receipt_id,
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "args_hash": args_hash,
                    "fingerprint": fingerprint,
                    "status": RECEIPT_STARTED,
                    "observation_json": "{}",
                    "error": "",
                    "created_at": now,
                    "updated_at": now,
                },
            )
        except sqlite3.IntegrityError:
            raced = self.get_execute_receipt(tenant_id, fingerprint)
            if raced is None:
                raise
            return raced, False
        stored = self.get_execute_receipt(tenant_id, fingerprint)
        if stored is None:
            raise HitlExecuteBlocked(
                "Execute receipt was not stored. Refusing to run the gated tool.",
            )
        return stored, True

    def finish_execute_receipt(
        self,
        receipt_id: str,
        *,
        status: str,
        observation: Dict[str, Any],
        error: str = "",
    ) -> None:
        """Record the outcome. This is the durable 'tool is done' write.

        ``succeeded`` means ``invoke`` returned ok. ``failed`` means it returned
        an error after the tool was entered (or schema failed after the claim).
        Leaving the row ``started`` is reserved for a crash before this update.
        """
        if status not in (RECEIPT_SUCCEEDED, RECEIPT_FAILED):
            raise ValueError("execute receipt finish status must be succeeded or failed")
        err = error or ""
        if len(err) > _MAX_RECEIPT_ERROR_CHARS:
            err = err[: _MAX_RECEIPT_ERROR_CHARS - 3] + "..."
        self.db.update(
            "hitl_execute_receipts",
            {
                "status": status,
                "observation_json": observation_snippet(observation),
                "error": err,
                "updated_at": utcnow(),
            },
            "id=?",
            (receipt_id,),
        )

    def resolve_execute_receipt(
        self,
        *,
        tenant_id: str,
        task_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        reexecute: bool,
    ) -> Dict[str, Any]:
        """Claim or reuse the receipt for this pause.

        Returns ``{action, receipt, observation}``. ``action`` is ``invoke`` or
        ``reuse``. ``started`` without ``reexecute`` raises
        :class:`HitlExecuteBlocked` and does not invoke.

        Policy:

        * no row — insert ``started``, caller invokes, then ``finish``
        * ``succeeded`` or ``failed`` — reuse the stored observation; never invoke
          again, including when ``reexecute`` is true
        * ``started`` — fail closed unless ``reexecute`` (explicit re-approve).
          That second invoke may duplicate a side effect that ran before the
          crash. It updates the same row; it does not insert another one.
        """
        args_hash, fingerprint = execute_fingerprint(task_id, tool_name, arguments)
        receipt, created = self.claim_execute_receipt(
            tenant_id=tenant_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_hash=args_hash,
            fingerprint=fingerprint,
        )
        status = receipt.get("status")
        if status in (RECEIPT_SUCCEEDED, RECEIPT_FAILED):
            observation = loads(receipt.get("observation_json") or "", None)
            if not isinstance(observation, dict):
                raise HitlExecuteBlocked(
                    f"Gated tool {tool_name!r} has a {status} execute receipt "
                    f"({receipt.get('id')}) with no reusable observation. "
                    "Refusing to run the side effect again.",
                    receipt_id=str(receipt.get("id") or ""),
                )
            if reexecute:
                logger.info(
                    "hitl_execute_reexecute_ignored task_id=%s tool=%s status=%s",
                    task_id,
                    tool_name,
                    status,
                )
            return {"action": "reuse", "receipt": receipt, "observation": observation}
        if status == RECEIPT_STARTED and (created or reexecute):
            if reexecute and not created:
                logger.warning(
                    "hitl_execute_reexecute task_id=%s tool=%s receipt_id=%s",
                    task_id,
                    tool_name,
                    receipt.get("id") or "",
                )
            return {"action": "invoke", "receipt": receipt, "observation": None}
        if status == RECEIPT_STARTED:
            receipt_id = str(receipt.get("id") or "")
            raise HitlExecuteBlocked(
                f"Gated tool {tool_name!r} has an in-flight execute receipt ({receipt_id}) "
                "with status 'started'. The outcome was not recorded, so the side effect "
                "is not run again. The task stays awaiting_human. "
                'POST /resume with {"reexecute": true} to explicitly re-approve another '
                "attempt; that may duplicate the side effect. A succeeded receipt is never "
                "run again.",
                receipt_id=receipt_id,
            )
        raise HitlExecuteBlocked(
            f"Gated tool {tool_name!r} has an execute receipt with unexpected status "
            f"{status!r}. Refusing to run the side effect.",
            receipt_id=str(receipt.get("id") or ""),
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
