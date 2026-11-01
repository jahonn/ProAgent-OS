"""OIDC mode is a sketch. It does not call an identity provider.

``PROAGENT_AUTH_MODE=api_key`` (the default) never imports a JWT library and
never reads the issuer settings.

``PROAGENT_AUTH_MODE=oidc`` refuses to start until issuer, audience, and
client id are set. Those three values are the settings a future
authorization-code login will need. This module does not perform that login,
does not fetch JWKS, and does not read a client secret.

Bearer JWTs are accepted only when the process has registered a
``TokenVerifier``. Nothing registers one at import. ``HmacJwksVerifier`` is
an in-memory HS256 stand-in for tests and local experiments. It is not
selected by an environment variable. Production verification, when it is
built, is RS256 (or the issuer's advertised alg) against the issuer's JWKS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Mapping, Protocol

from proagent_os.config import Settings
from proagent_os.rbac import Role

# (settings attribute, environment variable). Order is the startup error order.
OIDC_SETTINGS = (
    ("oidc_issuer", "PROAGENT_OIDC_ISSUER"),
    ("oidc_audience", "PROAGENT_OIDC_AUDIENCE"),
    ("oidc_client_id", "PROAGENT_OIDC_CLIENT_ID"),
)

OIDC_VERIFIER_MISSING = (
    "OIDC bearer tokens are not accepted yet. "
    "The admin key and tenant API keys still work. "
    "Register a TokenVerifier before a JWT is accepted. "
    "This process does not contact an identity provider."
)


class AuthConfigError(RuntimeError):
    """``PROAGENT_AUTH_MODE=oidc`` is selected without the settings it requires."""


class InvalidToken(Exception):
    """A bearer token was presented and rejected. ``detail`` is safe to return."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class TokenVerifier(Protocol):
    """Trust boundary for OIDC bearers.

    ``verify`` returns JWT claims (a mapping) or raises ``InvalidToken``.
    The caller checks issuer, audience, expiry, and role again.
    """

    def verify(self, token: str) -> Mapping[str, Any]: ...


_verifier: TokenVerifier | None = None


def set_token_verifier(verifier: TokenVerifier | None) -> None:
    """Install a process-global verifier. ``None`` restores the default (reject)."""
    global _verifier
    _verifier = verifier


def get_token_verifier() -> TokenVerifier | None:
    return _verifier


def reset_token_verifier() -> None:
    set_token_verifier(None)


def missing_oidc_env(settings: Settings) -> list[str]:
    missing: list[str] = []
    for attr, env in OIDC_SETTINGS:
        if not str(getattr(settings, attr) or "").strip():
            missing.append(env)
    return missing


def require_auth_config(settings: Settings) -> None:
    """No-op for ``api_key``. Raise ``AuthConfigError`` when OIDC is incomplete.

    The API (``create_app``) and the worker (``prepare``) both call this
    before serving. A registered ``TokenVerifier`` does not skip the check.
    """
    mode = str(settings.auth_mode or "").strip().lower()
    if mode == "api_key":
        return
    if mode != "oidc":
        raise AuthConfigError(
            f"PROAGENT_AUTH_MODE={settings.auth_mode!r} is not supported. Use api_key or oidc."
        )
    missing = missing_oidc_env(settings)
    if not missing:
        return
    names = ", ".join(missing)
    raise AuthConfigError(
        f"PROAGENT_AUTH_MODE=oidc is missing {names}. "
        "Set each of them, or set PROAGENT_AUTH_MODE=api_key. "
        "This sketch does not contact an identity provider and does not read a client secret. "
        "Signature checks are not bundled; register a TokenVerifier in-process (see docs/AUTH.md)."
    )


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    if not data or any(ch.isspace() for ch in data):
        raise ValueError("empty base64url")
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def parse_jwt_unverified(token: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Split a compact JWT. This does not check the signature."""
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidToken("Bearer token is not a compact JWT") from exc
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise InvalidToken("Bearer token is not a compact JWT (header.payload.signature)")
    try:
        header = json.loads(b64url_decode(parts[0]))
        payload = json.loads(b64url_decode(parts[1]))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        raise InvalidToken("JWT header or payload is not valid JSON") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise InvalidToken("JWT header and payload must be JSON objects")
    return header, payload, parts[2]


def claims_from_payload(
    payload: Mapping[str, Any],
    *,
    issuer: str,
    audience: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Check the claim shape this sketch understands.

    Required: ``iss`` equal to the configured issuer, ``aud`` equal to the
    configured audience (a string or a list that contains it), ``sub``,
    ``role`` (``admin``, ``tenant_operator``, or ``tenant_readonly``), and
    ``exp`` (unix seconds, not expired). Tenant roles also require
    ``tenant_id``. ``name`` is optional.

    There is no clock skew. A future JWKS verifier should keep these checks.
    """
    if not isinstance(payload, Mapping):
        raise InvalidToken("JWT payload must be a JSON object")
    if payload.get("iss") != issuer:
        raise InvalidToken("JWT issuer does not match PROAGENT_OIDC_ISSUER")
    aud = payload.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if audience not in audiences:
        raise InvalidToken("JWT audience does not match PROAGENT_OIDC_AUDIENCE")
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise InvalidToken("JWT sub is required")
    try:
        role = Role(payload.get("role"))
    except ValueError:
        raise InvalidToken(
            "JWT role must be admin, tenant_operator, or tenant_readonly"
        ) from None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        raise InvalidToken("JWT exp is required and must be a unix timestamp")
    current = time.time() if now is None else now
    if float(exp) <= current:
        raise InvalidToken("JWT is expired")
    tenant_id = payload.get("tenant_id")
    if role is not Role.ADMIN:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise InvalidToken("JWT tenant_id is required for tenant roles")
    elif tenant_id is not None and not isinstance(tenant_id, str):
        raise InvalidToken("JWT tenant_id must be a string when present")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise InvalidToken("JWT name must be a string when present")
    return {
        "iss": issuer,
        "aud": audience,
        "sub": sub,
        "role": role.value,
        "exp": float(exp),
        "tenant_id": tenant_id if isinstance(tenant_id, str) else None,
        "name": name,
    }


def sign_hs256_jwt(payload: Mapping[str, Any], *, secret: bytes, kid: str) -> str:
    """Compact HS256 JWT. Tests and the in-memory verifier share this encoding.

    The secret is an argument. This function does not read the environment
    and does not store the secret.
    """
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    header_part = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_part = b64url_encode(
        json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = b64url_encode(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header_part}.{payload_part}.{signature}"


class HmacJwksVerifier:
    """HS256 over an in-memory ``kid`` → secret map.

    This stands in for an issuer JWKS document in tests. It does not open a
    socket. Do not register it in a deployment: it is not OIDC, and HS256
    with a shared secret is not the verification this project intends to ship.
    """

    def __init__(self, keys: Mapping[str, bytes], *, issuer: str, audience: str) -> None:
        self._keys = dict(keys)
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> Mapping[str, Any]:
        header, payload, signature = parse_jwt_unverified(token)
        alg = header.get("alg")
        if alg != "HS256":
            raise InvalidToken(
                f"JWT alg {alg!r} is not accepted by the HS256 test verifier"
            )
        kid = header.get("kid")
        secret = self._keys.get(kid) if isinstance(kid, str) else None
        if secret is None:
            raise InvalidToken("JWT kid does not match the in-memory JWKS mock")
        signing_input = token.rsplit(".", 1)[0].encode("ascii")
        expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
        try:
            actual = b64url_decode(signature)
        except ValueError as exc:
            raise InvalidToken("JWT signature is not base64url") from exc
        if len(actual) != len(expected) or not hmac.compare_digest(actual, expected):
            raise InvalidToken("JWT signature does not match the in-memory JWKS mock")
        return claims_from_payload(payload, issuer=self._issuer, audience=self._audience)
