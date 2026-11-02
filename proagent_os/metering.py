"""SQLite usage-event stub.

Rows are local totals for a tenant. Nothing here contacts a payment provider,
creates an invoice, or charges a card. ``GET /api/v1/usage`` labels the
response ``billing: stub``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Dict, Iterator, List, Optional

from proagent_os.db import get_db, new_id, utcnow

# Stable kinds. Append a new name here and in the docs together; do not
# rename one that has already been written to usage_events.
# eval_run: one row when a golden-eval call finishes. quantity is the number
# of cases in that call (run_golden_task passes 1). See docs/EVAL.md.
USAGE_KINDS = ("llm_tokens", "kb_search", "recommendation_execute", "eval_run")

BILLING_STUB = "stub"
BILLING_DETAIL = (
    "Usage metering stub. Totals are stored in SQLite. "
    "This is not a payment integration and does not create charges."
)

_tenant_id: ContextVar[Optional[str]] = ContextVar("proagent_metering_tenant", default=None)


def current_tenant_id() -> Optional[str]:
    return _tenant_id.get()


def bind_tenant(tenant_id: str) -> Token[Optional[str]]:
    return _tenant_id.set(tenant_id)


def unbind_tenant(token: Token[Optional[str]]) -> None:
    _tenant_id.reset(token)


@contextmanager
def use_tenant(tenant_id: str) -> Iterator[None]:
    token = bind_tenant(tenant_id)
    try:
        yield
    finally:
        unbind_tenant(token)


def llm_token_quantity(response: Any) -> int:
    """Tokens to store for one chat response.

    Prefers ``usage.total_tokens``, then ``prompt_tokens`` + ``completion_tokens``.
    The mock provider sends no usage block; the quantity is then
    ``len(content) // 4`` (at least 1 when there is text or a tool call) so an
    offline run still writes a row. That estimate is not a provider bill.
    """
    usage = getattr(response, "usage", None) or {}
    if usage.get("total_tokens") is not None:
        return max(0, int(usage["total_tokens"]))
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt or completion:
        return max(0, prompt + completion)
    text = getattr(response, "content", None) or ""
    tool_calls = getattr(response, "tool_calls", None) or []
    if not text and tool_calls:
        text = " ".join(getattr(call, "name", "") or "" for call in tool_calls)
    estimated = len(text) // 4
    if estimated > 0:
        return estimated
    if text or tool_calls:
        return 1
    return 0


def record_usage(tenant_id: str, kind: str, quantity: int = 1) -> Dict[str, Any]:
    """Insert one usage event. ``quantity`` is a non-negative integer."""
    if kind not in USAGE_KINDS:
        allowed = ", ".join(USAGE_KINDS)
        raise ValueError(f"usage kind must be one of: {allowed}")
    if not tenant_id:
        raise ValueError("tenant_id is required")
    amount = int(quantity)
    if amount < 0:
        raise ValueError("quantity must be >= 0")
    row = {
        "id": new_id("use"),
        "tenant_id": tenant_id,
        "kind": kind,
        "quantity": amount,
        "created_at": utcnow(),
    }
    get_db().insert("usage_events", row)
    return row


def usage_report(tenant_id: Optional[str]) -> Dict[str, Any]:
    """Aggregate counts. ``tenant_id`` None returns every tenant."""
    db = get_db()
    if tenant_id is None:
        rows = db.fetchall(
            """
            SELECT tenant_id, kind, COUNT(*) AS events, SUM(quantity) AS quantity
            FROM usage_events
            GROUP BY tenant_id, kind
            ORDER BY tenant_id, kind
            """
        )
    else:
        rows = db.fetchall(
            """
            SELECT kind, COUNT(*) AS events, SUM(quantity) AS quantity
            FROM usage_events
            WHERE tenant_id=?
            GROUP BY kind
            ORDER BY kind
            """,
            (tenant_id,),
        )
    totals: List[Dict[str, Any]] = []
    for row in rows:
        item: Dict[str, Any] = {
            "kind": row["kind"],
            "events": int(row["events"]),
            "quantity": int(row["quantity"]),
        }
        if tenant_id is None:
            item["tenant_id"] = row["tenant_id"]
        totals.append(item)
    return {
        "billing": BILLING_STUB,
        "payment": None,
        "detail": BILLING_DETAIL,
        "tenant_id": tenant_id,
        "totals": totals,
    }
