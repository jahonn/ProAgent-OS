from __future__ import annotations

from fastapi import APIRouter, HTTPException

from proagent_os.auth import AdminDep, TenantDep, enforce
from proagent_os.db import get_db, new_id, utcnow
from proagent_os.keys import audit_issued, log_key_action, mint_api_key, revoke_tenant_key, rotate_tenant_key
from proagent_os.models.schemas import (
    ApiKeyRevokeOut,
    ApiKeyRotateOut,
    TenantCreate,
    TenantOut,
    TenantRoleUpdate,
    TenantUpdate,
)
from proagent_os.professional.brand.store import BrandStore
from proagent_os.rbac import Permission, Role, load_role_overrides, set_tenant_role

router = APIRouter()

_ME_FIELDS = (
    "id",
    "name",
    "goals",
    "constraints",
    "tone",
    "created_at",
    "api_key",
    "key_status",
    "role",
)


def _missing_tenant(exc: KeyError) -> HTTPException:
    return HTTPException(404, "tenant not found")


@router.post("", response_model=TenantOut)
def create_tenant(body: TenantCreate, admin: AdminDep) -> dict:
    enforce(admin, Permission.TENANT_CREATE)
    tid = new_id("ten")
    api_key = mint_api_key()
    created_at = utcnow()
    with get_db().connect() as conn:
        conn.execute(
            """
            INSERT INTO tenants (id, name, api_key, goals, constraints, tone, created_at, key_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (tid, body.name, api_key, body.goals, body.constraints, body.tone, created_at),
        )
        prefix, digest = audit_issued(
            conn, tenant_id=tid, actor="admin", api_key=api_key, created_at=created_at
        )
    log_key_action(event="issued", tenant_id=tid, actor="admin", prefix=prefix, digest=digest)
    row = get_db().fetchone("SELECT * FROM tenants WHERE id=?", (tid,))
    return row  # type: ignore[return-value]


@router.get("/me")
def get_me(tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        return tenant
    return {k: tenant[k] for k in _ME_FIELDS if k in tenant}


@router.patch("/me")
def update_me(body: TenantUpdate, tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Admin has no brand profile; use a tenant key")
    store = BrandStore()
    updated = store.update(tenant["id"], goals=body.goals, constraints=body.constraints, tone=body.tone)
    return updated.__dict__ if updated else {}


@router.post("/me/api-key/rotate", response_model=ApiKeyRotateOut)
def rotate_my_api_key(tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Admin has no tenant API key; rotate a tenant by id")
    enforce(tenant, Permission.API_KEY_ROTATE_SELF)
    return rotate_tenant_key(tenant["id"], actor=tenant["id"])


@router.post("/me/api-key/revoke", response_model=ApiKeyRevokeOut)
def revoke_my_api_key(tenant: TenantDep) -> dict:
    if tenant.get("is_admin"):
        raise HTTPException(400, "Admin has no tenant API key; revoke a tenant by id")
    enforce(tenant, Permission.API_KEY_REVOKE_SELF)
    return revoke_tenant_key(tenant["id"], actor=tenant["id"])


@router.post("/{tenant_id}/api-key/rotate", response_model=ApiKeyRotateOut)
def rotate_tenant_api_key(tenant_id: str, admin: AdminDep) -> dict:
    enforce(admin, Permission.API_KEY_ROTATE_ANY)
    try:
        return rotate_tenant_key(tenant_id, actor="admin")
    except KeyError as exc:
        raise _missing_tenant(exc) from None


@router.post("/{tenant_id}/api-key/revoke", response_model=ApiKeyRevokeOut)
def revoke_tenant_api_key(tenant_id: str, admin: AdminDep) -> dict:
    enforce(admin, Permission.API_KEY_REVOKE_ANY)
    try:
        return revoke_tenant_key(tenant_id, actor="admin")
    except KeyError as exc:
        raise _missing_tenant(exc) from None


@router.put("/{tenant_id}/role")
def put_tenant_role(tenant_id: str, body: TenantRoleUpdate, admin: AdminDep) -> dict:
    """Set the API-key role. Does not change an OIDC token's ``role`` claim."""
    enforce(admin, Permission.TENANT_LIST)
    try:
        return set_tenant_role(tenant_id, body.role)
    except KeyError as exc:
        raise _missing_tenant(exc) from None


@router.get("")
def list_tenants(admin: AdminDep) -> list:
    enforce(admin, Permission.TENANT_LIST)
    rows = get_db().fetchall(
        "SELECT id, name, goals, constraints, tone, key_status, created_at FROM tenants ORDER BY created_at DESC"
    )
    overrides = load_role_overrides()
    for row in rows:
        row["role"] = overrides.get(row["id"], Role.TENANT_OPERATOR.value)
    return rows
