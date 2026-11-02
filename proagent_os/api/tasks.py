from __future__ import annotations

from fastapi import APIRouter, HTTPException

from proagent_os.auth import TenantDep, enforce
from proagent_os.db import get_db, loads
from proagent_os.harness.hitl import (
    HitlExecuteBlocked,
    TaskHitlService,
    public_execute_receipts,
)
from proagent_os.models.schemas import (
    TaskCreate,
    TaskHitlNote,
    TaskListItem,
    TaskPause,
    TaskRead,
)
from proagent_os.rbac import Permission
from proagent_os.services import get_services

router = APIRouter()


def _decode_task_row(row: dict) -> dict:
    row = dict(row)
    row["plan"] = loads(row.pop("plan_json", "[]"), [])
    row["trace"] = loads(row.pop("trace_json", "[]"), [])
    row["pending_action"] = loads(row.pop("pending_action_json", "{}"), {})
    row["hitl_checkpoint"] = loads(row.pop("hitl_checkpoint_json", "{}"), {})
    return row


def _get_owned_task(task_id: str, tenant: dict) -> dict:
    row = get_db().fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(404, "task not found")
    if not tenant.get("is_admin") and row["tenant_id"] != tenant["id"]:
        raise HTTPException(404, "task not found")
    return row


def _with_execute_receipts(rows: list[dict]) -> list[dict]:
    """Attach safe execute-receipt summaries. One indexed read for the page.

    Scope is the task's own ``tenant_id`` (already enforced by the caller).
    This does not claim a receipt or invoke a tool.
    """
    keys = [(str(row["tenant_id"]), str(row["id"])) for row in rows]
    grouped = TaskHitlService().list_execute_receipts_many(keys)
    enriched: list[dict] = []
    for row in rows:
        item = dict(row)
        item["hitl_execute_receipts"] = public_execute_receipts(
            grouped.get(item["id"], [])
        )
        enriched.append(item)
    return enriched


@router.post("")
async def create_task(body: TaskCreate, tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key to run tasks")
    svc = get_services()
    result = await svc.orchestrator.run_task(
        tenant["id"],
        body.input,
        conversation_id=body.conversation_id,
    )
    return result


@router.get("", response_model=list[TaskListItem])
def list_tasks(tenant: TenantDep, limit: int = 50) -> list:
    """List tasks for this tenant. Admin sees every tenant.

    Each item includes ``hitl_execute_receipts``: tool name, fingerprint
    prefix, args hash, status, timestamps, and a capped observation and
    error. Raw arguments are omitted. Same read access as this list.
    """
    tid = tenant["id"]
    if tenant.get("is_admin"):
        rows = get_db().fetchall(
            "SELECT id, tenant_id, status, input, pause_reason, hitl_decision, "
            "created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    else:
        rows = get_db().fetchall(
            "SELECT id, tenant_id, status, input, pause_reason, hitl_decision, "
            "created_at, updated_at FROM tasks WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tid, limit),
        )
    return _with_execute_receipts(rows)


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: str, tenant: TenantDep) -> dict:
    """Return one task, including ``hitl_execute_receipts``.

    Same tenant scope as before: the owning tenant (operator or readonly)
    or the admin key. Another tenant receives 404. The receipt array is a
    summary (fingerprint prefix, tool name, status, timestamps, capped
    observation and error). It does not include raw arguments or secrets.
    """
    row = _get_owned_task(task_id, tenant)
    return _with_execute_receipts([_decode_task_row(row)])[0]


@router.post("/{task_id}/pause")
def pause_task(task_id: str, body: TaskPause, tenant: TenantDep) -> dict:
    """Explicit pause: running → awaiting_human. Reuses recommendation.decide RBAC."""
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    enforce(tenant, Permission.RECOMMENDATION_DECIDE)
    row = _get_owned_task(task_id, tenant)
    if row["tenant_id"] != tenant["id"]:
        raise HTTPException(404, "task not found")
    try:
        return get_services().hitl.pause_task(
            tenant["id"],
            task_id,
            reason=body.reason,
            pending_action=body.pending_action,
            actor=tenant["id"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, tenant: TenantDep, body: TaskHitlNote | None = None) -> dict:
    """Approve a paused run.

    Default (``PROAGENT_HITL_RESUME_CONTINUE=true``): when the orchestrator stored
    a checkpoint, re-enter the specialist loop with the human decision injected,
    then finish remaining plan steps (OpenAI / LangGraph shaped). If
    ``PROAGENT_HITL_EXECUTE_ON_APPROVE`` is also true (default) and the pause
    stored a gated-tool snapshot, that tool runs once through ToolRegistry
    (schema checked again) before the specialist continues. A durable execute
    receipt is written before that invoke. A second resume reuses a stored
    success or failure and does not invoke again. An in-flight (``started``)
    receipt returns 409 unless ``reexecute`` is true and the task is still
    ``awaiting_human``. If the task is already ``running`` and that receipt
    is ``succeeded`` or ``failed``, this same route re-injects the stored
    observation and finishes the open checkpoint. It does not invoke. A
    ``running`` task with a ``started`` receipt returns 409 even when
    ``reexecute`` is true. Without a checkpoint, or when the continue flag
    is false, marks ``completed`` (legacy stub) and does not run the tool.
    """
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    enforce(tenant, Permission.RECOMMENDATION_DECIDE)
    row = _get_owned_task(task_id, tenant)
    if row["tenant_id"] != tenant["id"]:
        raise HTTPException(404, "task not found")
    note = (body.note if body else "") or ""
    reexecute = bool(body.reexecute) if body else False
    try:
        return await get_services().orchestrator.resume_after_hitl(
            tenant["id"],
            task_id,
            actor=tenant["id"],
            note=note,
            reexecute=reexecute,
        )
    except HitlExecuteBlocked as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except KeyError:
        raise HTTPException(404, "task not found") from None


@router.post("/{task_id}/reject")
def reject_task(task_id: str, tenant: TenantDep, body: TaskHitlNote | None = None) -> dict:
    """Reject a paused run: awaiting_human → cancelled.

    Also cancels a ``running`` task that still has an open HITL checkpoint
    (force-cancel after a crash, or a ``started`` receipt while ``running``).
    A ``running`` task with no checkpoint is refused. Never continues the
    specialist loop and never executes a gated tool.
    """
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    enforce(tenant, Permission.RECOMMENDATION_DECIDE)
    row = _get_owned_task(task_id, tenant)
    if row["tenant_id"] != tenant["id"]:
        raise HTTPException(404, "task not found")
    note = (body.note if body else "") or ""
    try:
        return get_services().hitl.reject_task(
            tenant["id"], task_id, actor=tenant["id"], note=note
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
