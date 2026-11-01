"""Read API for the usage-metering stub.

Tenant bearers see their own totals. The admin key sees every tenant, or one
tenant when ``tenant_id`` is set. The payload is labeled ``billing: stub``.
There is no Stripe client and no charge is created.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from proagent_os.auth import TenantDep
from proagent_os.db import get_db
from proagent_os.metering import usage_report

router = APIRouter()


@router.get("")
def read_usage(tenant: TenantDep, tenant_id: str | None = None) -> dict:
    if tenant.get("is_admin"):
        if tenant_id is not None:
            found = get_db().fetchone("SELECT id FROM tenants WHERE id=?", (tenant_id,))
            if not found:
                raise HTTPException(404, "tenant not found")
            return usage_report(tenant_id)
        return usage_report(None)
    if tenant_id is not None and tenant_id != tenant["id"]:
        raise HTTPException(403, "Cannot read another tenant's usage")
    return usage_report(tenant["id"])
