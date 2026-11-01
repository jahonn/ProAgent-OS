"""Request authentication.

Default mode is ``api_key``: the admin key and tenant API keys behave as they
did before roles existed. Each principal gains a ``role``.

``oidc`` mode still accepts those keys, so a configured-but-unwired issuer
does not lock operators out. Any other bearer is a JWT, and only a registered
``TokenVerifier`` can accept it. Incomplete OIDC settings never reach this
function: startup raises ``AuthConfigError`` first.
"""

from __future__ import annotations

from typing import Annotated, Any, Mapping, Optional

from fastapi import Depends, Header, HTTPException, status

from proagent_os.config import get_settings
from proagent_os.db import get_db
from proagent_os.keys import key_usable
from proagent_os.oidc import (
    OIDC_VERIFIER_MISSING,
    InvalidToken,
    claims_from_payload,
    get_token_verifier,
)
from proagent_os.rbac import Permission, Role, has_permission, resolve_tenant_role


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


def enforce(principal: Mapping[str, Any], permission: Permission) -> None:
    """403 when ``principal``'s role does not include ``permission``."""
    role = str(principal.get("role") or "")
    if has_permission(role, permission):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Role {role or 'unknown'} cannot {permission.value}",
    )


def _admin_principal(token: str, *, auth_method: str) -> dict:
    return {
        "id": "admin",
        "name": "Admin",
        "api_key": token if auth_method == "admin_key" else "",
        "is_admin": True,
        "key_status": "active",
        "role": Role.ADMIN.value,
        "auth_method": auth_method,
    }


def _tenant_api_key_principal(row: dict) -> dict:
    row["is_admin"] = False
    row["role"] = resolve_tenant_role(row["id"]).value
    row["auth_method"] = "api_key"
    return row


def _principal_from_claims(claims: Mapping[str, Any]) -> dict:
    role = Role(claims["role"])
    sub = str(claims["sub"])
    if role is Role.ADMIN:
        principal = _admin_principal("", auth_method="oidc")
        principal["name"] = claims.get("name") or sub
        principal["sub"] = sub
        return principal
    tenant_id = str(claims["tenant_id"])
    row = get_db().fetchone("SELECT * FROM tenants WHERE id=?", (tenant_id,))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC token tenant_id does not match a tenant",
        )
    # Drop the stored API key so an SSO caller does not receive that secret.
    row["api_key"] = ""
    row["is_admin"] = False
    row["role"] = role.value
    row["auth_method"] = "oidc"
    row["sub"] = sub
    return row


def _principal_from_oidc_bearer(token: str) -> dict:
    verifier = get_token_verifier()
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=OIDC_VERIFIER_MISSING,
        )
    try:
        raw = verifier.verify(token)
        settings = get_settings()
        claims = claims_from_payload(
            raw,
            issuer=settings.oidc_issuer.strip(),
            audience=settings.oidc_audience.strip(),
        )
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.detail,
        ) from None
    return _principal_from_claims(claims)


async def require_tenant(
    authorization: Annotated[Optional[str], Header()] = None,
) -> dict:
    token = extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    settings = get_settings()
    if token == settings.admin_key:
        return _admin_principal(token, auth_method="admin_key")
    row = get_db().fetchone("SELECT * FROM tenants WHERE api_key = ?", (token,))
    # Revoked and unknown secrets share one error so a revoked key is not confirmed.
    if row and key_usable(row):
        return _tenant_api_key_principal(row)
    # Only a compact JWT is an OIDC bearer. Revoked keys and other secrets
    # stay on the same 401 as an unknown key, so a revoked key is not confirmed.
    if settings.auth_mode == "oidc" and _looks_like_compact_jwt(token):
        return _principal_from_oidc_bearer(token)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def _looks_like_compact_jwt(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


async def require_admin(tenant: Annotated[dict, Depends(require_tenant)]) -> dict:
    if tenant.get("role") != Role.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin key required")
    return tenant


TenantDep = Annotated[dict, Depends(require_tenant)]
AdminDep = Annotated[dict, Depends(require_admin)]
