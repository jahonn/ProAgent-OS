"""Roles on the default API-key path, and the OIDC sketch (no live IdP)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import ValidationError

from proagent_os.config import get_settings
from proagent_os.db import get_db
from proagent_os.oidc import (
    AuthConfigError,
    HmacJwksVerifier,
    InvalidToken,
    get_token_verifier,
    set_token_verifier,
    sign_hs256_jwt,
)
from proagent_os.rbac import Permission, has_permission, set_tenant_role
from proagent_os.services import get_services

ADMIN = {"Authorization": "Bearer test-admin-key"}
ISSUER = "https://idp.example/realms/proagent"
AUDIENCE = "proagent-os"
CLIENT_ID = "proagent-os"
KID = "test-1"
SECRET = b"test-only-hmac-secret"


def _use(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _client():
    # Import after the fixture has selected api_key, so a later test can flip
    # the mode without the module-level app object failing the import.
    from fastapi.testclient import TestClient

    from proagent_os.main import create_app

    return TestClient(create_app())


def _create(client, name: str = "Acme") -> dict:
    resp = client.post("/api/v1/tenants", headers=ADMIN, json={"name": name, "tone": "concise"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _pending(tenant_id: str, title: str = "Review") -> str:
    rec = get_services().recommendations.create(tenant_id, title)
    assert rec["status"] == "pending"
    return rec["id"]


def _status(rec_id: str) -> str:
    row = get_db().fetchone("SELECT status FROM recommendations WHERE id=?", (rec_id,))
    assert row is not None
    return row["status"]


def _claims(*, role: str, tenant_id: str | None, exp: float | None = None, **extra: Any) -> dict:
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"user-{role}",
        "role": role,
        "exp": time.time() + 3600 if exp is None else exp,
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    payload.update(extra)
    return payload


def _jwt(*, role: str, tenant_id: str | None, secret: bytes = SECRET, kid: str = KID, **extra: Any) -> str:
    return sign_hs256_jwt(_claims(role=role, tenant_id=tenant_id, **extra), secret=secret, kid=kid)


def _oidc_env(monkeypatch: pytest.MonkeyPatch, *, mode: str = "oidc") -> None:
    _use(
        monkeypatch,
        PROAGENT_AUTH_MODE=mode,
        PROAGENT_OIDC_ISSUER=ISSUER,
        PROAGENT_OIDC_AUDIENCE=AUDIENCE,
        PROAGENT_OIDC_CLIENT_ID=CLIENT_ID,
    )


class _ClaimVerifier:
    """Test double: one fixed bearer maps to claims. No signature and no network."""

    def __init__(self, token: str, claims: dict) -> None:
        self.token = token
        self.claims = claims

    def verify(self, token: str) -> dict:
        if token != self.token:
            raise InvalidToken("mock verifier rejected the token")
        return self.claims


def test_role_permission_matrix():
    assert has_permission("tenant_readonly", Permission.USAGE_READ)
    assert has_permission("tenant_readonly", Permission.RECOMMENDATION_READ)
    assert not has_permission("tenant_readonly", Permission.RECOMMENDATION_DECIDE)
    assert not has_permission("tenant_readonly", Permission.API_KEY_ROTATE_SELF)
    assert not has_permission("tenant_readonly", Permission.API_KEY_REVOKE_SELF)
    assert not has_permission("tenant_readonly", Permission.TENANT_LIST)
    assert has_permission("tenant_operator", Permission.RECOMMENDATION_DECIDE)
    assert has_permission("tenant_operator", Permission.API_KEY_ROTATE_SELF)
    assert not has_permission("tenant_operator", Permission.TENANT_LIST)
    assert not has_permission("tenant_operator", Permission.API_KEY_ROTATE_ANY)
    assert has_permission("admin", Permission.TENANT_LIST)
    assert has_permission("admin", Permission.TENANT_CREATE)
    assert has_permission("admin", Permission.USAGE_READ)
    assert has_permission("admin", Permission.API_KEY_ROTATE_ANY)
    assert not has_permission("admin", Permission.RECOMMENDATION_DECIDE)
    assert not has_permission("nope", Permission.USAGE_READ)
    assert get_token_verifier() is None


def test_api_key_mode_is_unchanged():
    assert get_settings().auth_mode == "api_key"
    with _client() as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["auth_mode"] == "api_key"

        created = _create(client, "Default Co")
        key = created["api_key"]
        headers = {"Authorization": f"Bearer {key}"}
        me = client.get("/api/v1/tenants/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["role"] == "tenant_operator"
        assert me.json()["api_key"] == key

        admin_me = client.get("/api/v1/tenants/me", headers=ADMIN)
        assert admin_me.status_code == 200
        assert admin_me.json()["role"] == "admin"
        assert admin_me.json()["is_admin"] is True

        listed = client.get("/api/v1/tenants", headers=headers)
        assert listed.status_code == 403
        assert listed.json()["detail"] == "Admin key required"
        admin_list = client.get("/api/v1/tenants", headers=ADMIN)
        assert admin_list.status_code == 200
        row = next(item for item in admin_list.json() if item["id"] == created["id"])
        assert row["role"] == "tenant_operator"
        assert "api_key" not in row

        rec_id = _pending(created["id"], "Ship it")
        admin_approve = client.post(f"/api/v1/recommendations/{rec_id}/approve", headers=ADMIN)
        assert admin_approve.status_code == 400
        assert admin_approve.json()["detail"] == "Use a tenant API key"
        approved = client.post(f"/api/v1/recommendations/{rec_id}/approve", headers=headers)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "executed"

        usage = client.get("/api/v1/usage", headers=headers)
        assert usage.status_code == 200
        assert usage.json()["billing"] == "stub"
        admin_usage = client.get("/api/v1/usage", headers=ADMIN)
        assert admin_usage.status_code == 200

        rotated = client.post("/api/v1/tenants/me/api-key/rotate", headers=headers)
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["api_key"] != key

        unknown = client.get("/api/v1/tenants/me", headers={"Authorization": "Bearer not-a-key"})
        assert unknown.status_code == 401
        assert unknown.json()["detail"] == "Invalid API key"
        shaped = client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
        assert shaped.status_code == 401
        assert shaped.json()["detail"] == "Invalid API key"


def test_readonly_cannot_approve_rotate_or_revoke():
    with _client() as client:
        created = _create(client, "Readonly Co")
        tid = created["id"]
        key = created["api_key"]
        headers = {"Authorization": f"Bearer {key}"}
        other = _create(client, "Other Co")

        denied = client.put(
            f"/api/v1/tenants/{tid}/role",
            headers=headers,
            json={"role": "tenant_readonly"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Admin key required"

        missing = client.put(
            "/api/v1/tenants/ten_missing/role",
            headers=ADMIN,
            json={"role": "tenant_readonly"},
        )
        assert missing.status_code == 404

        bad = client.put(
            f"/api/v1/tenants/{tid}/role",
            headers=ADMIN,
            json={"role": "admin"},
        )
        assert bad.status_code == 422

        updated = client.put(
            f"/api/v1/tenants/{tid}/role",
            headers=ADMIN,
            json={"role": "tenant_readonly"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["role"] == "tenant_readonly"
        assert updated.json()["tenant_id"] == tid

        me = client.get("/api/v1/tenants/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["role"] == "tenant_readonly"
        assert me.json()["api_key"] == key

        listed = client.get("/api/v1/tenants", headers=ADMIN)
        row = next(item for item in listed.json() if item["id"] == tid)
        assert row["role"] == "tenant_readonly"

        rec_id = _pending(tid, "Do not ship")
        listed_recs = client.get("/api/v1/recommendations", headers=headers)
        assert listed_recs.status_code == 200
        assert any(item["id"] == rec_id for item in listed_recs.json())

        for path, body in (
            (f"/api/v1/recommendations/{rec_id}/approve", None),
            (f"/api/v1/recommendations/{rec_id}/reject", None),
            (f"/api/v1/recommendations/{rec_id}/decide", {"status": "approved"}),
        ):
            resp = client.post(path, headers=headers, json=body)
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == "Role tenant_readonly cannot recommendation.decide"
        assert _status(rec_id) == "pending"

        for path in (
            "/api/v1/tenants/me/api-key/rotate",
            "/api/v1/tenants/me/api-key/revoke",
        ):
            resp = client.post(path, headers=headers)
            assert resp.status_code == 403, resp.text
        assert "rotate_self" in client.post("/api/v1/tenants/me/api-key/rotate", headers=headers).json()["detail"]
        assert client.get("/api/v1/tenants/me", headers=headers).status_code == 200

        usage = client.get("/api/v1/usage", headers=headers)
        assert usage.status_code == 200
        assert usage.json()["tenant_id"] == tid
        foreign = client.get("/api/v1/usage", headers=headers, params={"tenant_id": other["id"]})
        assert foreign.status_code == 403

        restored = client.put(
            f"/api/v1/tenants/{tid}/role",
            headers=ADMIN,
            json={"role": "tenant_operator"},
        )
        assert restored.status_code == 200
        approved = client.post(f"/api/v1/recommendations/{rec_id}/approve", headers=headers)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "executed"


def test_set_tenant_role_rejects_admin_and_unknown_tenant():
    with pytest.raises(ValueError, match="admin"):
        set_tenant_role("ten_missing", "admin")
    with pytest.raises(ValueError, match="tenant_operator or tenant_readonly"):
        set_tenant_role("ten_missing", "superuser")


def test_oidc_mode_without_config_fails_clearly(monkeypatch: pytest.MonkeyPatch):
    from proagent_os.main import create_app
    from proagent_os.worker import prepare

    _use(monkeypatch, PROAGENT_AUTH_MODE="oidc")
    with pytest.raises(AuthConfigError, match="PROAGENT_OIDC_ISSUER") as missing:
        create_app()
    text = str(missing.value)
    assert "PROAGENT_OIDC_AUDIENCE" in text
    assert "PROAGENT_OIDC_CLIENT_ID" in text
    assert "does not contact an identity provider" in text
    assert "client secret" in text

    with pytest.raises(AuthConfigError, match="PROAGENT_OIDC_ISSUER"):
        prepare()

    _use(
        monkeypatch,
        PROAGENT_AUTH_MODE="oidc",
        PROAGENT_OIDC_ISSUER=ISSUER,
        PROAGENT_OIDC_AUDIENCE="   ",
        PROAGENT_OIDC_CLIENT_ID="",
    )
    with pytest.raises(AuthConfigError, match="PROAGENT_OIDC_AUDIENCE") as partial:
        create_app()
    partial_text = str(partial.value)
    assert "PROAGENT_OIDC_CLIENT_ID" in partial_text
    assert "PROAGENT_OIDC_ISSUER" not in partial_text

    set_token_verifier(_ClaimVerifier("mock.token.value", _claims(role="admin", tenant_id=None)))
    with pytest.raises(AuthConfigError, match="PROAGENT_OIDC_AUDIENCE"):
        create_app()


def test_unknown_auth_mode_is_rejected(monkeypatch: pytest.MonkeyPatch):
    _use(monkeypatch, PROAGENT_AUTH_MODE="saml")
    with pytest.raises(ValidationError):
        get_settings()


def test_oidc_without_verifier_rejects_jwt_and_keeps_api_keys(monkeypatch: pytest.MonkeyPatch):
    from proagent_os.main import create_app

    _oidc_env(monkeypatch, mode="OIDC")
    assert get_settings().auth_mode == "oidc"
    assert get_token_verifier() is None
    with _client() as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["auth_mode"] == "oidc"

        created = _create(client, "Still keyed")
        headers = {"Authorization": f"Bearer {created['api_key']}"}
        me = client.get("/api/v1/tenants/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["role"] == "tenant_operator"
        assert client.get("/api/v1/tenants", headers=ADMIN).status_code == 200

        typo = client.get("/api/v1/tenants/me", headers={"Authorization": "Bearer not-a-key"})
        assert typo.status_code == 401
        assert typo.json()["detail"] == "Invalid API key"
        revoked = client.post("/api/v1/tenants/me/api-key/revoke", headers=headers)
        assert revoked.status_code == 200, revoked.text
        dead = client.get("/api/v1/tenants/me", headers=headers)
        assert dead.status_code == 401
        assert dead.json()["detail"] == "Invalid API key"

        token = _jwt(role="tenant_operator", tenant_id=created["id"])
        rejected = client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token}"})
        assert rejected.status_code == 401
        detail = rejected.json()["detail"]
        assert "does not contact an identity provider" in detail
        assert "TokenVerifier" in detail
        assert token not in rejected.text
        assert created["api_key"] not in rejected.text

    # create_app is what startup calls; importing it above already exercised the check.
    create_app()


def test_mock_verifier_readonly_cannot_approve(monkeypatch: pytest.MonkeyPatch):
    _oidc_env(monkeypatch)
    with _client() as client:
        created = _create(client, "Mock SSO")
        tid = created["id"]
        rec_id = _pending(tid, "Hold")
        set_token_verifier(
            _ClaimVerifier("mock.token.value", _claims(role="tenant_readonly", tenant_id=tid))
        )
        headers = {"Authorization": "Bearer mock.token.value"}
        me = client.get("/api/v1/tenants/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["role"] == "tenant_readonly"
        assert me.json()["api_key"] == ""
        assert created["api_key"] not in me.text
        listed = client.get("/api/v1/recommendations", headers=headers)
        assert listed.status_code == 200
        denied = client.post(f"/api/v1/recommendations/{rec_id}/approve", headers=headers)
        assert denied.status_code == 403
        assert _status(rec_id) == "pending"
        usage = client.get("/api/v1/usage", headers=headers)
        assert usage.status_code == 200
        assert usage.json()["tenant_id"] == tid

        rejected = client.get("/api/v1/tenants/me", headers={"Authorization": "Bearer other.token.value"})
        assert rejected.status_code == 401
        assert rejected.json()["detail"] == "mock verifier rejected the token"


def test_hmac_jwks_mock_enforces_role_and_signature(monkeypatch: pytest.MonkeyPatch):
    _oidc_env(monkeypatch)
    other_secret = b"another-test-secret"
    set_token_verifier(HmacJwksVerifier({KID: SECRET, "other": other_secret}, issuer=ISSUER, audience=AUDIENCE))
    with _client() as client:
        created = _create(client, "JWT Co")
        tid = created["id"]
        key_headers = {"Authorization": f"Bearer {created['api_key']}"}
        # The API-key role stays operator. A readonly JWT does not change that key.
        client.put(
            f"/api/v1/tenants/{tid}/role",
            headers=ADMIN,
            json={"role": "tenant_readonly"},
        )
        rec_id = _pending(tid, "From SSO")

        readonly = _jwt(role="tenant_readonly", tenant_id=tid)
        ro_headers = {"Authorization": f"Bearer {readonly}"}
        assert client.get("/api/v1/recommendations", headers=ro_headers).status_code == 200
        denied = client.post(f"/api/v1/recommendations/{rec_id}/approve", headers=ro_headers)
        assert denied.status_code == 403
        assert _status(rec_id) == "pending"
        # The tenant API key follows the override, not the JWT role.
        assert client.post(
            f"/api/v1/recommendations/{rec_id}/approve", headers=key_headers
        ).status_code == 403

        operator = _jwt(role="tenant_operator", tenant_id=tid)
        op_headers = {"Authorization": f"Bearer {operator}"}
        me = client.get("/api/v1/tenants/me", headers=op_headers)
        assert me.status_code == 200, me.text
        assert me.json()["role"] == "tenant_operator"
        assert me.json()["api_key"] == ""
        assert created["api_key"] not in me.text
        approved = client.post(f"/api/v1/recommendations/{rec_id}/approve", headers=op_headers)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "executed"

        admin_token = _jwt(role="admin", tenant_id=None)
        listed = client.get("/api/v1/tenants", headers={"Authorization": f"Bearer {admin_token}"})
        assert listed.status_code == 200
        assert any(item["id"] == tid for item in listed.json())

        tampered = readonly[:-2] + ("aa" if not readonly.endswith("aa") else "bb")
        bad_sig = client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {tampered}"})
        assert bad_sig.status_code == 401
        assert "in-memory JWKS mock" in bad_sig.json()["detail"]
        assert tampered not in bad_sig.text

        unknown_kid = _jwt(role="tenant_operator", tenant_id=tid, kid="missing")
        missing_kid = client.get(
            "/api/v1/tenants/me", headers={"Authorization": f"Bearer {unknown_kid}"}
        )
        assert missing_kid.status_code == 401
        assert "kid" in missing_kid.json()["detail"]

        expired = _jwt(role="tenant_operator", tenant_id=tid, exp=time.time() - 10)
        assert client.get(
            "/api/v1/tenants/me", headers={"Authorization": f"Bearer {expired}"}
        ).json()["detail"] == "JWT is expired"

        wrong_aud = sign_hs256_jwt(
            _claims(role="tenant_operator", tenant_id=tid, aud="other-app"),
            secret=SECRET,
            kid=KID,
        )
        wrong = client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {wrong_aud}"})
        assert wrong.status_code == 401
        assert "PROAGENT_OIDC_AUDIENCE" in wrong.json()["detail"]

        none_alg = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ4In0.e30"
        none = client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {none_alg}"})
        assert none.status_code == 401
        assert "none" in none.json()["detail"]
        assert "HS256 test verifier" in none.json()["detail"]


def test_claims_accept_audience_list():
    from proagent_os.oidc import claims_from_payload

    claims = claims_from_payload(
        {
            "iss": ISSUER,
            "aud": ["other", AUDIENCE],
            "sub": "user-1",
            "role": "admin",
            "exp": time.time() + 60,
        },
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    assert claims["role"] == "admin"
    assert claims["aud"] == AUDIENCE
    with pytest.raises(InvalidToken, match="expired"):
        claims_from_payload(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "user-1",
                "role": "admin",
                "exp": time.time() - 1,
            },
            issuer=ISSUER,
            audience=AUDIENCE,
        )


def test_auth_docs_do_not_carry_a_client_secret():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in (".env.example", "docs/AUTH.md", "proagent_os/oidc.py", "README.md"):
        text = (root / rel).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "begin private key" not in lowered
        assert "proagent_oidc_client_secret" not in lowered
        assert "client_secret=" not in lowered
