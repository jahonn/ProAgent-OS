"""Tests for the /health endpoint."""

from __future__ import annotations

import pytest

from proagent_os.config import get_settings
from proagent_os.ratelimit import reset_rate_limiter


def _use(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_rate_limiter()


def test_health_includes_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /health returns status, a non-empty version, and the configured LLM provider."""
    _use(monkeypatch, PROAGENT_LLM_PROVIDER="mock")
    from fastapi.testclient import TestClient

    from proagent_os.main import create_app

    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body.get("version"), str) and body["version"]
    assert body.get("llm") == "mock"
