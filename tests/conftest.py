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
    monkeypatch.setenv("PROAGENT_LLM_PROVIDER", "mock")
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
    monkeypatch.setenv("PROAGENT_AGENTS_CONFIG", str(Path("config/agents.yaml").resolve()))
    monkeypatch.setenv(
        "PROAGENT_SKILLS_DIRS",
        ",".join(
            [
                str(Path("skills").resolve()),
                str(Path("verticals/example/skills").resolve()),
                str(Path("verticals/support_desk/skills").resolve()),
            ]
        ),
    )

    from proagent_os.config import get_settings
    from proagent_os.db import reset_db
    from proagent_os.llm.factory import reset_llm_cache
    from proagent_os.services import reset_services

    get_settings.cache_clear()
    reset_llm_cache()
    reset_db(db_path)
    reset_services()
    from proagent_os.ratelimit import reset_rate_limiter

    reset_rate_limiter()
    yield
    get_settings.cache_clear()
    reset_llm_cache()
