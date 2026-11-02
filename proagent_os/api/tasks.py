from __future__ import annotations

from fastapi import APIRouter, HTTPException

from proagent_os.auth import TenantDep, enforce
from proagent_os.db import get_db, loads
from proagent_os.models.schemas import TaskCreate, TaskHitlNote, TaskPause
from proagent_os.rbac import Permission
from proagent_os.services import get_services

router = APIRouter()


def _decode_task_row(row: dict) -> dict:
    row = dict(row)
    row["plan"] = loads(row.pop("plan_json", "[]"), [])
    row["trace"] = loads(row.pop("trace_json", "[]"), [])
    row["pending_action"] = loads(row.pop("pending_action_json", "{}"), {})
    return row


def _get_owned_task(task_id: str, tenant: dict) -> dict:
    row = get_db().fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(404, "task not found")
    if not tenant.get("is_admin") and row["tenant_id"] != tenant["id"]:
        raise HTTPException(404, "task not found")
    return row


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


@router.get("")
def list_tasks(tenant: TenantDep, limit: int = 50) -> list:
    tid = tenant["id"]
    if tenant.get("is_admin"):
        return get_db().fetchall(
            "SELECT id, tenant_id, status, input, pause_reason, hitl_decision, "
            "created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return get_db().fetchall(
        "SELECT id, tenant_id, status, input, pause_reason, hitl_decision, "
        "created_at, updated_at FROM tasks WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
        (tid, limit),
    )


@router.get("/{task_id}")
def get_task(task_id: str, tenant: TenantDep) -> dict:
    row = _get_owned_task(task_id, tenant)
    return _decode_task_row(row)


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
def resume_task(task_id: str, tenant: TenantDep, body: TaskHitlNote | None = None) -> dict:
    """Approve a paused run: awaiting_human → completed (stub; no mid-loop continue)."""
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    enforce(tenant, Permission.RECOMMENDATION_DECIDE)
    row = _get_owned_task(task_id, tenant)
    if row["tenant_id"] != tenant["id"]:
        raise HTTPException(404, "task not found")
    note = (body.note if body else "") or ""
    try:
        return get_services().hitl.resume_task(
            tenant["id"], task_id, actor=tenant["id"], note=note
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/{task_id}/reject")
def reject_task(task_id: str, tenant: TenantDep, body: TaskHitlNote | None = None) -> dict:
    """Reject a paused run: awaiting_human → cancelled."""
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
