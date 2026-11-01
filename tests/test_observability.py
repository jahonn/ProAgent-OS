"""OpenTelemetry no-op path and the SQLite usage-metering stub."""

from __future__ import annotations

import builtins
import sys
import traceback
from typing import Any, List

import pytest

from proagent_os.config import Settings, get_settings
from proagent_os.db import get_db, new_id, utcnow
from proagent_os.llm.base import LLMMessage, LLMResponse
from proagent_os.llm.factory import get_llm
from proagent_os.metering import (
    BILLING_STUB,
    llm_token_quantity,
    record_usage,
    usage_report,
    use_tenant,
)
from proagent_os.observability import SPAN_HTTP_REQUEST, SPAN_KB_SEARCH, reset_observability, span
from proagent_os.services import get_services

ADMIN = {"Authorization": "Bearer test-admin-key"}


def _enable(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_observability()


def _tenant(name: str = "Usage Co") -> str:
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": name,
            "api_key": new_id("pk"),
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
            "key_status": "active",
        },
    )
    return tid


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class _FakeTracer:
    def __init__(self) -> None:
        self.names: List[str] = []
        self.spans: List[_FakeSpan] = []

    def start_as_current_span(self, name: str):
        tracer = self

        class _Bound:
            def __enter__(self) -> _FakeSpan:
                current = _FakeSpan()
                tracer.names.append(name)
                tracer.spans.append(current)
                return current

            def __exit__(self, *args: object) -> bool:
                return False

        return _Bound()


def _block_proagent_otel_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail if proagent_os imports OpenTelemetry.

    Current FastAPI builds import ``opentelemetry-api`` themselves. That
    package is not the SDK. This guard allows that import and rejects one
    whose stack passes through ``proagent_os``.
    """
    real_import = builtins.__import__

    def guarding(name: str, globals=None, locals=None, fromlist=(), level: int = 0):  # noqa: A002
        if name.split(".", 1)[0] == "opentelemetry":
            for frame in traceback.extract_stack()[:-1]:
                path = (frame.filename or "").replace("\\", "/")
                if "/proagent_os/" in path or path.endswith("/proagent_os"):
                    raise AssertionError(f"{path} imported {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarding)


def test_otel_defaults_are_off():
    assert Settings.model_fields["otel_enabled"].default is False
    assert Settings.model_fields["otel_exporter"].default == "console"


def test_disabled_path_does_not_import_opentelemetry(monkeypatch: pytest.MonkeyPatch):
    _enable(monkeypatch, PROAGENT_OTEL_ENABLED="false")
    _block_proagent_otel_import(monkeypatch)

    def refuse_configure() -> None:
        raise AssertionError("OpenTelemetry was configured while disabled")

    monkeypatch.setattr("proagent_os.observability._configure_tracer", refuse_configure)
    with span("llm.chat", {"llm.provider": "mock"}) as current:
        assert current is None

    tid = _tenant()
    get_services().kb.ingest_text(tid, "note", "Note", "conversion rate target is 2.5 percent")
    hits = get_services().kb.search(tid, "conversion", top_k=2)
    assert hits

    from fastapi.testclient import TestClient

    from proagent_os.main import create_app

    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert not any(
        name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk.")
        or name.startswith("opentelemetry.exporter")
        for name in sys.modules
    )


def test_enabled_without_sdk_stays_noop(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _enable(monkeypatch, PROAGENT_OTEL_ENABLED="true", PROAGENT_OTEL_EXPORTER="none")
    real_import = builtins.__import__
    sdk_imports = {"n": 0}

    def broken(name: str, globals=None, locals=None, fromlist=(), level: int = 0):  # noqa: A002
        if name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk.") or name.startswith(
            "opentelemetry.exporter"
        ):
            sdk_imports["n"] += 1
            raise ImportError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", broken)
    caplog.set_level("WARNING", logger="proagent.otel")
    with span(SPAN_KB_SEARCH) as current:
        assert current is None
    with span(SPAN_KB_SEARCH) as current:
        assert current is None
    assert sdk_imports["n"] == 1
    assert "Spans are no-ops" in caplog.text


def test_enabled_span_delegates_to_tracer(monkeypatch: pytest.MonkeyPatch):
    _enable(monkeypatch, PROAGENT_OTEL_ENABLED="true", PROAGENT_OTEL_EXPORTER="none")
    tracer = _FakeTracer()
    monkeypatch.setattr("proagent_os.observability._configure_tracer", lambda: tracer)
    with span(SPAN_HTTP_REQUEST, {"http.method": "GET", "skipped": None}) as current:
        assert current is tracer.spans[0]
        current.set_attribute("http.status_code", 204)
    assert tracer.names == [SPAN_HTTP_REQUEST]
    assert tracer.spans[0].attributes["http.method"] == "GET"
    assert "skipped" not in tracer.spans[0].attributes
    assert tracer.spans[0].attributes["http.status_code"] == 204


def test_http_middleware_span_when_enabled(monkeypatch: pytest.MonkeyPatch):
    _enable(monkeypatch, PROAGENT_OTEL_ENABLED="true", PROAGENT_OTEL_EXPORTER="none")
    tracer = _FakeTracer()
    monkeypatch.setattr("proagent_os.observability._configure_tracer", lambda: tracer)

    from fastapi.testclient import TestClient

    from proagent_os.main import create_app

    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert tracer.names == [SPAN_HTTP_REQUEST]
    assert tracer.spans[0].attributes["http.path"] == "/health"
    assert tracer.spans[0].attributes["http.status_code"] == 200


def test_llm_token_quantity_prefers_provider_usage():
    assert llm_token_quantity(LLMResponse(content="hi", usage={"total_tokens": 42})) == 42
    assert (
        llm_token_quantity(
            LLMResponse(content="hi", usage={"prompt_tokens": 3, "completion_tokens": 4})
        )
        == 7
    )
    assert llm_token_quantity(LLMResponse(content="abcdEFGH", usage={})) == 2
    assert llm_token_quantity(LLMResponse(content=None, usage={})) == 0


def test_record_usage_rejects_unknown_kind_and_negative_quantity():
    tid = _tenant()
    with pytest.raises(ValueError, match="usage kind"):
        record_usage(tid, "stripe_charge", 1)
    with pytest.raises(ValueError, match="quantity"):
        record_usage(tid, "kb_search", -1)


def test_usage_insert_and_aggregate_are_tenant_scoped():
    first = _tenant("First")
    second = _tenant("Second")
    record_usage(first, "kb_search", 1)
    record_usage(first, "kb_search", 1)
    record_usage(first, "llm_tokens", 12)
    record_usage(second, "llm_tokens", 4)
    record_usage(second, "recommendation_execute", 1)

    report = usage_report(first)
    assert report["billing"] == BILLING_STUB
    assert report["payment"] is None
    assert "not a payment integration" in report["detail"]
    assert report["tenant_id"] == first
    kinds = {row["kind"]: row for row in report["totals"]}
    assert kinds["kb_search"] == {"kind": "kb_search", "events": 2, "quantity": 2}
    assert kinds["llm_tokens"]["quantity"] == 12
    assert "recommendation_execute" not in kinds

    everyone = usage_report(None)
    assert everyone["tenant_id"] is None
    assert everyone["billing"] == "stub"
    quantities = {(row["tenant_id"], row["kind"]): row["quantity"] for row in everyone["totals"]}
    assert quantities[(first, "llm_tokens")] == 12
    assert quantities[(second, "llm_tokens")] == 4
    assert quantities[(second, "recommendation_execute")] == 1


def test_usage_api_scopes_tenant_and_admin():
    from fastapi.testclient import TestClient

    from proagent_os.main import create_app

    first = _tenant("Api First")
    second = _tenant("Api Second")
    first_key = get_db().fetchone("SELECT api_key FROM tenants WHERE id=?", (first,))["api_key"]
    second_key = get_db().fetchone("SELECT api_key FROM tenants WHERE id=?", (second,))["api_key"]
    record_usage(first, "kb_search", 1)
    record_usage(second, "llm_tokens", 9)

    with TestClient(create_app()) as client:
        missing = client.get("/api/v1/usage")
        assert missing.status_code == 401

        own = client.get("/api/v1/usage", headers={"Authorization": f"Bearer {first_key}"})
        assert own.status_code == 200, own.text
        body = own.json()
        assert body["billing"] == "stub"
        assert body["payment"] is None
        assert body["tenant_id"] == first
        assert body["totals"] == [{"kind": "kb_search", "events": 1, "quantity": 1}]

        forbidden = client.get(
            "/api/v1/usage",
            headers={"Authorization": f"Bearer {first_key}"},
            params={"tenant_id": second},
        )
        assert forbidden.status_code == 403

        admin_all = client.get("/api/v1/usage", headers=ADMIN)
        assert admin_all.status_code == 200, admin_all.text
        all_body = admin_all.json()
        assert all_body["tenant_id"] is None
        assert all_body["billing"] == "stub"
        seen = {(row["tenant_id"], row["kind"]) for row in all_body["totals"]}
        assert (first, "kb_search") in seen
        assert (second, "llm_tokens") in seen

        admin_one = client.get("/api/v1/usage", headers=ADMIN, params={"tenant_id": second})
        assert admin_one.status_code == 200, admin_one.text
        assert admin_one.json()["totals"] == [{"kind": "llm_tokens", "events": 1, "quantity": 9}]

        missing_tenant = client.get(
            "/api/v1/usage", headers=ADMIN, params={"tenant_id": "ten_missing"}
        )
        assert missing_tenant.status_code == 404


def test_kb_search_and_recommendation_record_usage(monkeypatch: pytest.MonkeyPatch):
    _enable(monkeypatch, PROAGENT_OTEL_ENABLED="true")
    tracer = _FakeTracer()
    monkeypatch.setattr("proagent_os.observability._configure_tracer", lambda: tracer)

    tid = _tenant()
    svc = get_services()
    svc.kb.ingest_text(tid, "note", "Note", "conversion rate target is 2.5 percent")
    hits = svc.kb.search(tid, "conversion", top_k=2)
    assert hits
    assert SPAN_KB_SEARCH in tracer.names
    search_span = tracer.spans[tracer.names.index(SPAN_KB_SEARCH)]
    assert search_span.attributes["tenant_id"] == tid
    assert search_span.attributes["kb.hits"] == len(hits)

    pending = svc.recommendations.create(tid, "Review findings", "A follow-up is due")
    rejected = svc.recommendations.decide(tid, pending["id"], "rejected")
    assert rejected["status"] == "rejected"

    approved = svc.recommendations.create(tid, "Ship the follow-up", "Approve to execute")
    decided = svc.recommendations.decide(tid, approved["id"], "approved")
    assert decided["status"] == "executed"
    assert decided["execution"]["mode"] == "noop"

    rows = get_db().fetchall(
        "SELECT kind, quantity FROM usage_events WHERE tenant_id=? ORDER BY rowid",
        (tid,),
    )
    assert rows == [
        {"kind": "kb_search", "quantity": 1},
        {"kind": "recommendation_execute", "quantity": 1},
    ]
    assert "recommendation.approve" in tracer.names
    assert "recommendation.execute" in tracer.names


@pytest.mark.asyncio
async def test_llm_chat_records_tokens_for_the_bound_tenant():
    tid = _tenant()
    before = get_db().fetchone("SELECT COUNT(*) AS n FROM usage_events")["n"]
    await get_llm().chat([LLMMessage(role="user", content="Hello metering")])
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM usage_events")["n"] == before

    with use_tenant(tid):
        await get_llm().chat([LLMMessage(role="user", content="Hello metering")])
    row = get_db().fetchone(
        "SELECT kind, quantity, tenant_id FROM usage_events WHERE tenant_id=?",
        (tid,),
    )
    assert row["kind"] == "llm_tokens"
    assert row["quantity"] >= 1
    assert row["tenant_id"] == tid


@pytest.mark.asyncio
async def test_run_task_attributes_llm_tokens():
    tid = _tenant()
    result = await get_services().orchestrator.run_task(tid, "Summarize the conversion rate")
    assert result["status"] == "completed"
    rows = get_db().fetchall(
        "SELECT quantity FROM usage_events WHERE tenant_id=? AND kind='llm_tokens'",
        (tid,),
    )
    assert rows
    assert all(row["quantity"] >= 1 for row in rows)
