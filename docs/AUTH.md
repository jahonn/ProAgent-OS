# Authentication

ProAgent OS authenticates `/api/v1` with a bearer token. The default mode is the admin key plus one API key per tenant. Roles and an OIDC mode are a sketch on top of that path. They are not a production single sign-on integration.

No client secret belongs in this repository. This sketch does not read one.

## What works today

`PROAGENT_AUTH_MODE` defaults to `api_key`. Leave it there for the README curl commands, the mock demo, and CI.

| Bearer | Role | What it can do |
|--------|------|----------------|
| `PROAGENT_ADMIN_KEY` | `admin` | Create and list tenants, rotate or revoke any tenant key, read usage (one tenant or all). Same calls as before. |
| Tenant API key, no override | `tenant_operator` | That tenant's existing write APIs, including approve / reject / execute and self-service key rotate / revoke. |
| Tenant API key with an override | `tenant_readonly` | Read that tenant's recommendations and usage. The gated mutations below return 403. |

`GET /api/v1/tenants/me` includes `role`. `GET /api/v1/tenants` (admin) includes `role` and still omits the secret. `GET /health` includes `auth_mode`.

A tenant key is `tenant_operator` until an admin sets an override:

```bash
curl -s -X PUT http://localhost:8080/api/v1/tenants/ten_xxx/role \
  -H "Authorization: Bearer $PROAGENT_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"role":"tenant_readonly"}'
```

`tenant_operator` puts the key back. `admin` is not a value this endpoint accepts. The row lives in `tenant_role_overrides` (migration `0004_tenant_role_overrides`). The table cannot store `admin`. Role changes are logged as `tenant_role` with the tenant id and the role. They are not written to `tenant_api_key_audit`.

## Permission checks

`proagent_os.rbac` names the permissions. `proagent_os.auth.enforce` returns HTTP 403 `Role <role> cannot <permission>` when the principal does not have it. The check is on these routes:

| Route | Permission | Readonly |
|-------|------------|----------|
| `GET /api/v1/tenants`, `PUT /api/v1/tenants/{id}/role` | `tenant.list` | 403 `Admin key required` (not an admin) |
| `POST /api/v1/tenants` | `tenant.create` | same |
| `POST /api/v1/tenants/{id}/api-key/rotate` | `apikey.rotate_any` | same |
| `POST /api/v1/tenants/{id}/api-key/revoke` | `apikey.revoke_any` | same |
| `POST /api/v1/tenants/me/api-key/rotate` | `apikey.rotate_self` | 403 |
| `POST /api/v1/tenants/me/api-key/revoke` | `apikey.revoke_self` | 403 |
| `GET /api/v1/recommendations` | `recommendation.read` | allowed |
| `POST .../approve`, `.../reject`, `.../decide` | `recommendation.decide` | 403, and the row stays `pending` |
| `GET /api/v1/usage` | `usage.read` | allowed for that tenant only |

Approve, reject, and execute share `recommendation.decide`. An admin key on those routes is still HTTP 400 `Use a tenant API key`, which is the previous behavior. Usage for another tenant is still HTTP 403.

**Not gated yet.** Tasks, knowledge-base writes, monitors, skill invoke, brand update, and the rest of the tenant API still accept any usable tenant key, including `tenant_readonly`. Treating readonly as a global read-only policy is planned. It is not what this sketch does.

## OIDC mode (stub)

```bash
PROAGENT_AUTH_MODE=oidc
PROAGENT_OIDC_ISSUER=https://idp.example/realms/proagent
PROAGENT_OIDC_AUDIENCE=proagent-os
PROAGENT_OIDC_CLIENT_ID=proagent-os
```

`PROAGENT_OIDC_ISSUER` is the issuer URL a future login will use, and the exact `iss` claim a token must carry. `PROAGENT_OIDC_AUDIENCE` is the expected `aud`. `PROAGENT_OIDC_CLIENT_ID` is the public OAuth client id for a future authorization-code client. The API and the worker both refuse to start when `PROAGENT_AUTH_MODE=oidc` and any of the three is empty. The error names the missing variables and states that this process does not contact an identity provider.

There is no browser redirect, no authorization-code exchange, no session cookie, and no JWKS fetch. CI does not need a live IdP. The default `api_key` mode ignores the three variables.

When the three values are set, startup succeeds and **the admin key and tenant API keys still work**. A revoked key and any other non-JWT bearer still return 401 `Invalid API key`. A compact JWT (three dot-separated segments) is the OIDC bearer:

- No `TokenVerifier` is registered unless your process calls `set_token_verifier`. The response is HTTP 401 and says OIDC bearers are not accepted yet. The token is not echoed.
- A registered verifier's `verify(token)` returns claims or raises `InvalidToken`. ProAgent checks the claims again: `iss`, `aud` (string or list), `sub`, `role` (`admin`, `tenant_operator`, or `tenant_readonly`), `exp` (unix seconds, no clock skew), and `tenant_id` for tenant roles.
- `admin` becomes the same admin principal as the admin key, without copying the token into `api_key`.
- A tenant role loads that tenant. The API key stored for the tenant is cleared on the principal so `GET /tenants/me` does not return it. The token's `role` is the role for that request. It does not rewrite `tenant_role_overrides`, and the tenant API key keeps the override.

`HmacJwksVerifier` in `proagent_os.oidc` is an in-memory `kid` → HMAC secret map. Tests use it as a JWKS stand-in. It checks HS256 and does not open a socket. It is not selected by an environment variable. Do not register it in a deployment. The verifier you register is the trust boundary.

Rate-limit buckets for a JWT still hash the bearer. They are not keyed by tenant id yet.

## Planned

- Authorization-code login against `PROAGENT_OIDC_ISSUER`, with the client secret in a secret manager, not in git or in `.env.example`.
- Signature checks against the issuer's JWKS (RS256 or the alg the issuer advertises), not the HS256 test double.
- Group or claim mapping into the three roles, and an audit row when an override changes.
- `enforce` on every mutating route, so `tenant_readonly` cannot ingest, run a task, or update the brand profile.
- Rate-limit identity for an OIDC bearer taken from `tenant_id` / `sub` rather than a hash of the token.

Until that work lands, run `PROAGENT_AUTH_MODE=api_key`.
