"""Roles for the admin key and tenant API keys.

Three roles are enough to show the path. They are not a full policy language.

* ``admin`` — the bootstrap admin key, or a future IdP token that carries this role.
* ``tenant_operator`` — a normal tenant API key. This is today's write access.
* ``tenant_readonly`` — the same tenant, without the mutations this sketch gates.

A tenant key is ``tenant_operator`` unless ``tenant_role_overrides`` says
otherwise. That table cannot store ``admin``. An override does not apply to
an OIDC token; the token's ``role`` claim is the role for that request.

Permission checks in this module are pure. HTTP 403 lives in
``proagent_os.auth.enforce``.
"""

from __future__ import annotations

import logging
import sqlite3
from enum import Enum
from typing import Dict, Mapping

from proagent_os.db import get_db, utcnow

logger = logging.getLogger("proagent.rbac")


class Role(str, Enum):
    ADMIN = "admin"
    TENANT_OPERATOR = "tenant_operator"
    TENANT_READONLY = "tenant_readonly"


class Permission(str, Enum):
    TENANT_LIST = "tenant.list"
    TENANT_CREATE = "tenant.create"
    API_KEY_ROTATE_SELF = "apikey.rotate_self"
    API_KEY_REVOKE_SELF = "apikey.revoke_self"
    API_KEY_ROTATE_ANY = "apikey.rotate_any"
    API_KEY_REVOKE_ANY = "apikey.revoke_any"
    RECOMMENDATION_READ = "recommendation.read"
    RECOMMENDATION_DECIDE = "recommendation.decide"
    USAGE_READ = "usage.read"


# Admin does not decide recommendations. Those routes stay on a tenant key,
# which is the behavior callers already have.
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(
        {
            Permission.TENANT_LIST,
            Permission.TENANT_CREATE,
            Permission.API_KEY_ROTATE_ANY,
            Permission.API_KEY_REVOKE_ANY,
            Permission.USAGE_READ,
        }
    ),
    Role.TENANT_OPERATOR: frozenset(
        {
            Permission.API_KEY_ROTATE_SELF,
            Permission.API_KEY_REVOKE_SELF,
            Permission.RECOMMENDATION_READ,
            Permission.RECOMMENDATION_DECIDE,
            Permission.USAGE_READ,
        }
    ),
    Role.TENANT_READONLY: frozenset(
        {
            Permission.RECOMMENDATION_READ,
            Permission.USAGE_READ,
        }
    ),
}

_TENANT_KEY_ROLES = frozenset({Role.TENANT_OPERATOR, Role.TENANT_READONLY})


def has_permission(role: str | None, permission: Permission) -> bool:
    try:
        parsed = Role(role or "")
    except ValueError:
        return False
    return permission in ROLE_PERMISSIONS[parsed]


def effective_tenant_role(stored: str | None) -> Role:
    """Map a stored override to a tenant-key role. Anything else is operator."""
    try:
        parsed = Role(stored or "")
    except ValueError:
        return Role.TENANT_OPERATOR
    if parsed not in _TENANT_KEY_ROLES:
        return Role.TENANT_OPERATOR
    return parsed


def resolve_tenant_role(tenant_id: str) -> Role:
    row = get_db().fetchone(
        "SELECT role FROM tenant_role_overrides WHERE tenant_id=?",
        (tenant_id,),
    )
    if row is None:
        return Role.TENANT_OPERATOR
    return effective_tenant_role(row.get("role"))


def load_role_overrides() -> Dict[str, str]:
    rows = get_db().fetchall("SELECT tenant_id, role FROM tenant_role_overrides")
    return {
        row["tenant_id"]: effective_tenant_role(row.get("role")).value
        for row in rows
    }


def set_tenant_role(tenant_id: str, role: str) -> Dict[str, str]:
    """Record the API-key role for one tenant. ``admin`` is rejected.

    Raises ``KeyError`` when the tenant does not exist and ``ValueError``
    when ``role`` is not a tenant-key role.
    """
    try:
        parsed = Role(role)
    except ValueError as exc:
        raise ValueError("role must be tenant_operator or tenant_readonly") from exc
    if parsed not in _TENANT_KEY_ROLES:
        raise ValueError("admin is reserved for the admin key and a future IdP claim")
    found = get_db().fetchone("SELECT id FROM tenants WHERE id=?", (tenant_id,))
    if found is None:
        raise KeyError(tenant_id)
    now = utcnow()
    try:
        with get_db().connect() as conn:
            conn.execute(
                """
                INSERT INTO tenant_role_overrides (tenant_id, role, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    role=excluded.role,
                    updated_at=excluded.updated_at
                """,
                (tenant_id, parsed.value, now),
            )
    except sqlite3.IntegrityError as exc:
        raise KeyError(tenant_id) from exc
    logger.info("tenant_role tenant_id=%s role=%s", tenant_id, parsed.value)
    return {"tenant_id": tenant_id, "role": parsed.value, "updated_at": now}
