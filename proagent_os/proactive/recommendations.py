from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from proagent_os.config import get_settings
from proagent_os.db import dumps, get_db, loads, new_id, utcnow
from proagent_os.metering import record_usage
from proagent_os.observability import (
    SPAN_RECOMMENDATION_APPROVE,
    SPAN_RECOMMENDATION_EXECUTE,
    span,
)
from proagent_os.proactive.executors import ExecutionResult, build_executor

logger = logging.getLogger("proagent.recommendations")


ALLOWED_STATUS = {"pending", "approved", "rejected", "executed"}


class RecommendationService:
    def __init__(self) -> None:
        self.db = get_db()

    def list(
        self,
        tenant_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        if status:
            rows = self.db.fetchall(
                "SELECT * FROM recommendations WHERE tenant_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, status, limit),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM recommendations WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            )
        for r in rows:
            r["action_payload"] = loads(r.pop("action_payload_json", "{}"), {})
        return rows

    def get(self, tenant_id: str, rec_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetchone(
            "SELECT * FROM recommendations WHERE id=? AND tenant_id=?",
            (rec_id, tenant_id),
        )
        if not row:
            return None
        row["action_payload"] = loads(row.pop("action_payload_json", "{}"), {})
        return row

    def decide(
        self,
        tenant_id: str,
        rec_id: str,
        status: str,
        *,
        actor: str = "",
    ) -> Dict[str, Any]:
        """Apply a strict transition and, on approval, optionally run the executor.

        pending → approved | rejected
        approved → executed
        rejected and executed are terminal.

        Approval writes an audit row (`decided`). When ``PROAGENT_EXECUTOR_ON_APPROVE``
        is set and the mode is not ``off``, a successful executor advances the row to
        ``executed``. A failure leaves it ``approved`` and writes ``execute_failed``.
        Reject never calls the executor. The returned dict may include
        ``execution: {ok, mode, error?}`` when an executor ran.
        """
        if status not in {"approved", "rejected", "executed"}:
            raise ValueError("status must be approved|rejected|executed")
        row = self.get(tenant_id, rec_id)
        if not row:
            raise KeyError("recommendation not found")
        current = row["status"]
        self._require_transition(current, status)

        if status == "rejected":
            self._set_status(tenant_id, rec_id, "rejected", decided_at=utcnow())
            self._audit(
                tenant_id=tenant_id,
                recommendation_id=rec_id,
                event="decided",
                from_status=current,
                to_status="rejected",
                actor=actor,
                detail={"status": "rejected"},
            )
            return self.get(tenant_id, rec_id) or {}

        if status == "approved":
            with span(
                SPAN_RECOMMENDATION_APPROVE,
                {"tenant_id": tenant_id, "recommendation.id": rec_id},
            ):
                self._set_status(tenant_id, rec_id, "approved", decided_at=utcnow())
                self._audit(
                    tenant_id=tenant_id,
                    recommendation_id=rec_id,
                    event="decided",
                    from_status=current,
                    to_status="approved",
                    actor=actor,
                    detail={"status": "approved"},
                )
                updated = self.get(tenant_id, rec_id) or {}
                settings = get_settings()
                if settings.executor_on_approve and settings.executor_mode != "off":
                    execution = self._invoke_executor(updated, actor)
                    updated = self.get(tenant_id, rec_id) or updated
                    updated["execution"] = execution
                return updated

        settings = get_settings()
        if settings.executor_mode == "off":
            with span(
                SPAN_RECOMMENDATION_EXECUTE,
                {
                    "tenant_id": tenant_id,
                    "recommendation.id": rec_id,
                    "executor.mode": "off",
                },
            ):
                self._set_status(tenant_id, rec_id, "executed")
                self._audit(
                    tenant_id=tenant_id,
                    recommendation_id=rec_id,
                    event="executed",
                    from_status=current,
                    to_status="executed",
                    actor=actor,
                    detail={"mode": "off"},
                )
                record_usage(tenant_id, "recommendation_execute", 1)
                updated = self.get(tenant_id, rec_id) or {}
                updated["execution"] = {"ok": True, "mode": "off"}
                return updated

        execution = self._invoke_executor(row, actor)
        updated = self.get(tenant_id, rec_id) or {}
        updated["execution"] = execution
        return updated

    def _require_transition(self, current: str, status: str) -> None:
        allowed = {
            ("pending", "approved"),
            ("pending", "rejected"),
            ("approved", "executed"),
        }
        if (current, status) not in allowed:
            raise ValueError(f"invalid transition: {current} -> {status}")

    def _set_status(
        self,
        tenant_id: str,
        rec_id: str,
        status: str,
        *,
        decided_at: Optional[str] = None,
    ) -> None:
        data: Dict[str, Any] = {"status": status}
        if decided_at is not None:
            data["decided_at"] = decided_at
        self.db.update(
            "recommendations",
            data,
            "id=? AND tenant_id=?",
            (rec_id, tenant_id),
        )

    def _invoke_executor(self, row: Dict[str, Any], actor: str) -> Dict[str, Any]:
        settings = get_settings()
        mode = settings.executor_mode
        with span(
            SPAN_RECOMMENDATION_EXECUTE,
            {
                "tenant_id": row.get("tenant_id") or "",
                "recommendation.id": row.get("id") or "",
                "executor.mode": mode,
            },
        ):
            try:
                result = build_executor(settings).execute(row)
            except Exception as exc:  # noqa: BLE001 — config and delivery failures stay on the row
                logger.warning(
                    "executor failed for recommendation %s (%s)",
                    row.get("id"),
                    type(exc).__name__,
                )
                message = str(exc).strip() or type(exc).__name__
                result = ExecutionResult(ok=False, mode=mode, error=message[:240])
            if result.ok:
                self._set_status(row["tenant_id"], row["id"], "executed")
                self._audit(
                    tenant_id=row["tenant_id"],
                    recommendation_id=row["id"],
                    event="executed",
                    from_status="approved",
                    to_status="executed",
                    actor=actor,
                    detail=result.as_api(),
                )
                record_usage(row["tenant_id"], "recommendation_execute", 1)
            else:
                self._audit(
                    tenant_id=row["tenant_id"],
                    recommendation_id=row["id"],
                    event="execute_failed",
                    from_status="approved",
                    to_status="approved",
                    actor=actor,
                    detail=result.as_api(),
                )
            return result.as_api()

    def _audit(
        self,
        *,
        tenant_id: str,
        recommendation_id: str,
        event: str,
        from_status: Optional[str],
        to_status: Optional[str],
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.db.insert(
            "recommendation_audit",
            {
                "id": new_id("aud"),
                "tenant_id": tenant_id,
                "recommendation_id": recommendation_id,
                "event": event,
                "from_status": from_status,
                "to_status": to_status,
                "actor": actor,
                "detail_json": dumps(detail or {}),
                "created_at": utcnow(),
            },
        )

    def create(
        self,
        tenant_id: str,
        title: str,
        rationale: str = "",
        action: Optional[Dict[str, Any]] = None,
        monitor_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rec_id = new_id("rec")
        self.db.insert(
            "recommendations",
            {
                "id": rec_id,
                "tenant_id": tenant_id,
                "monitor_id": monitor_id,
                "task_id": task_id,
                "title": title,
                "rationale": rationale,
                "action_payload_json": dumps(action or {}),
                "status": "pending",
                "created_at": utcnow(),
                "decided_at": None,
            },
        )
        return self.get(tenant_id, rec_id) or {"id": rec_id}

    def acceptance_rate(self, tenant_id: str) -> Dict[str, Any]:
        rows = self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM recommendations WHERE tenant_id=? GROUP BY status",
            (tenant_id,),
        )
        counts = {r["status"]: r["n"] for r in rows}
        decided = counts.get("approved", 0) + counts.get("rejected", 0) + counts.get("executed", 0)
        approved_like = counts.get("approved", 0) + counts.get("executed", 0)
        rate = (approved_like / decided) if decided else None
        return {"counts": counts, "acceptance_rate": rate, "decided": decided}
