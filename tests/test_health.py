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


def test_openapi_info_version_matches_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /openapi.json info.version is proagent_os.__version__, same as /health."""
    _use(monkeypatch, PROAGENT_LLM_PROVIDER="mock")
    from fastapi.testclient import TestClient

    from proagent_os import __version__
    from proagent_os.main import create_app

    app = create_app()
    assert app.version == __version__
    client = TestClient(app)
    spec = client.get("/openapi.json")
    assert spec.status_code == 200, spec.text
    assert spec.json()["info"]["version"] == __version__
    health = client.get("/health")
    assert health.status_code == 200, health.text
    assert health.json()["version"] == __version__
