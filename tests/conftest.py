from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure test DB isolation before imports that cache settings
TEST_DB = Path("/tmp/proagent_test.db")


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("PROAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("PROAGENT_ADMIN_KEY", "test-admin-key")
    monkeypatch.setenv("PROAGENT_AUTH_MODE", "api_key")
    monkeypatch.setenv("PROAGENT_OIDC_ISSUER", "")
    monkeypatch.setenv("PROAGENT_OIDC_AUDIENCE", "")
    monkeypatch.setenv("PROAGENT_OIDC_CLIENT_ID", "")
    monkeypatch.setenv("PROAGENT_LLM_PROVIDER", "mock")
    # Default trajectory matcher: judge tests opt into llm explicitly.
    monkeypatch.setenv("PROAGENT_EVAL_JUDGE", "ordered_subset")
    # Default executor is noop: CI stays offline and approval still records execution.
    monkeypatch.setenv("PROAGENT_EXECUTOR_MODE", "noop")
    monkeypatch.setenv("PROAGENT_EXECUTOR_ON_APPROVE", "true")
    monkeypatch.setenv("PROAGENT_EXECUTOR_WEBHOOK_URL", "")
    monkeypatch.setenv("PROAGENT_EXECUTOR_WEBHOOK_TOKEN", "")
    monkeypatch.setenv("PROAGENT_EXECUTOR_WEBHOOK_TIMEOUT_S", "5")
    monkeypatch.setenv("PROAGENT_KB_RETRIEVAL", "bm25")
    monkeypatch.setenv("PROAGENT_KB_EMBED_BACKEND", "stub")
    monkeypatch.setenv("PROAGENT_KB_EMBED_DIMS", "64")
    monkeypatch.setenv("PROAGENT_KB_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("PROAGENT_KB_EMBED_BASE_URL", "")
    monkeypatch.setenv("PROAGENT_KB_EMBED_API_KEY", "")
    monkeypatch.setenv("PROAGENT_KB_HYBRID_ALPHA", "0.5")
    monkeypatch.setenv("PROAGENT_KB_VECTOR_STORE", "blob")
    monkeypatch.setenv("PROAGENT_LLM_API_KEY", "")
    monkeypatch.setenv("PROAGENT_LLM_BASE_URL", "https://api.openai.com/v1")
    # High ceiling so existing API tests do not trip the stub. Dedicated tests lower it.
    monkeypatch.setenv("PROAGENT_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("PROAGENT_RATE_LIMIT_REQUESTS", "10000")
    monkeypatch.setenv("PROAGENT_RATE_LIMIT_WINDOW_SECONDS", "60")
    # MCP stdio adapter stays off so pytest does not import it or spawn a server.
    monkeypatch.setenv("PROAGENT_MCP_ENABLED", "false")
    monkeypatch.setenv("PROAGENT_MCP_SERVERS", "")
    monkeypatch.setenv("PROAGENT_MCP_CALL_TIMEOUT_S", "15")
    # Tracing stays off so pytest does not import opentelemetry.
    monkeypatch.setenv("PROAGENT_OTEL_ENABLED", "false")
    monkeypatch.setenv("PROAGENT_OTEL_EXPORTER", "none")
    monkeypatch.setenv("PROAGENT_OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setenv("PROAGENT_OTEL_SERVICE_NAME", "proagent-os")
    monkeypatch.setenv("PROAGENT_COMPACTION_ENABLED", "true")
    monkeypatch.setenv("PROAGENT_COMPACTION_MAX_MESSAGES", "40")
    monkeypatch.setenv("PROAGENT_COMPACTION_MAX_CHARS", "24000")
    monkeypatch.setenv("PROAGENT_COMPACTION_KEEP_RECENT", "12")
    monkeypatch.setenv("PROAGENT_COMPACTION_SUMMARY_MAX_CHARS", "2000")
    # Default extractive: compaction tests stay offline unless a case opts into llm.
    monkeypatch.setenv("PROAGENT_COMPACTION_SUMMARIZER", "extractive")
    # Outer replan stub: default off so existing tests keep zero extra plan calls.
    monkeypatch.setenv("PROAGENT_REPLAN_ENABLED", "false")
    monkeypatch.setenv("PROAGENT_REPLAN_MAX", "1")
    monkeypatch.setenv("PROAGENT_REPLAN_FORCE", "false")
    # HITL mid-loop continue after approve (default on). Dedicated tests may flip it.
    monkeypatch.setenv("PROAGENT_HITL_RESUME_CONTINUE", "true")
    # Approve runs the paused gated tool once (default on). Dedicated tests may flip it.
    monkeypatch.setenv("PROAGENT_HITL_EXECUTE_ON_APPROVE", "true")
    monkeypatch.setenv("PROAGENT_AGENTS_CONFIG", str(Path("config/agents.yaml").resolve()))
    monkeypatch.setenv(
        "PROAGENT_SKILLS_DIRS",
        ",".join(
            [
                str(Path("skills").resolve()),
                str(Path("verticals/example/skills").resolve()),
                str(Path("verticals/support_desk/skills").resolve()),
                str(Path("verticals/ops_incident/skills").resolve()),
            ]
        ),
    )

    from proagent_os.config import get_settings
    from proagent_os.db import reset_db
    from proagent_os.llm.factory import reset_llm_cache
    from proagent_os.observability import reset_observability
    from proagent_os.oidc import reset_token_verifier
    from proagent_os.services import get_services, reset_services

    get_settings.cache_clear()
    reset_token_verifier()
    reset_observability()
    reset_llm_cache()
    reset_db(db_path)
    reset_services()
    from proagent_os.ratelimit import reset_rate_limiter

    reset_rate_limiter()
    yield
    if get_services.cache_info().currsize:
        try:
            get_services().tools.close()
        except Exception:
            pass
    reset_token_verifier()
    get_settings.cache_clear()
    reset_llm_cache()
