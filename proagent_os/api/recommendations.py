from __future__ import annotations

from fastapi import APIRouter, HTTPException

from proagent_os.auth import TenantDep, enforce
from proagent_os.models.schemas import RecommendationDecide
from proagent_os.rbac import Permission
from proagent_os.services import get_services

router = APIRouter()


@router.get("")
def list_recs(tenant: TenantDep, status: str | None = None) -> list:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    enforce(tenant, Permission.RECOMMENDATION_READ)
    return get_services().recommendations.list(tenant["id"], status=status)


@router.post("/{rec_id}/decide")
def decide(rec_id: str, body: RecommendationDecide, tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Use a tenant API key")
    enforce(tenant, Permission.RECOMMENDATION_DECIDE)
    try:
        return get_services().recommendations.decide(
            tenant["id"], rec_id, body.status, actor=tenant["id"]
        )
    except KeyError:
        raise HTTPException(404, "recommendation not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/{rec_id}/approve")
def approve(rec_id: str, tenant: TenantDep) -> dict:
    return decide(rec_id, RecommendationDecide(status="approved"), tenant)


@router.post("/{rec_id}/reject")
def reject(rec_id: str, tenant: TenantDep) -> dict:
    return decide(rec_id, RecommendationDecide(status="rejected"), tenant)
