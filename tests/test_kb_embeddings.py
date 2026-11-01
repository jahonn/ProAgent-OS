"""OpenAI-compatible embeddings backend. HTTP is mocked; nothing dials out."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from proagent_os.config import get_settings
from proagent_os.db import get_db, new_id, utcnow
from proagent_os.professional.kb.embeddings import (
    EmbeddingsConfigError,
    EmbeddingsError,
    get_embedder,
)
from proagent_os.professional.kb.retrieve import rank_chunks_with_embeddings, unpack_embedding
from proagent_os.services import get_services


def _tenant() -> str:
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "KB Co",
            "api_key": new_id("pk"),
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    return tid


def _use(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "proagent_os.professional.kb.embeddings.httpx.Client",
        client_factory,
    )


def _enable_openai(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    env = {
        "PROAGENT_KB_RETRIEVAL": "vector",
        "PROAGENT_KB_EMBED_BACKEND": "openai",
        "PROAGENT_KB_EMBED_DIMS": "4",
        "PROAGENT_KB_EMBED_MODEL": "text-embedding-3-small",
        "PROAGENT_KB_EMBED_BASE_URL": "https://embed.example/v1/",
        "PROAGENT_KB_EMBED_API_KEY": "test-embed-key",
        "PROAGENT_LLM_API_KEY": "llm-key-should-not-be-used",
        "PROAGENT_LLM_BASE_URL": "https://llm.example/v1",
    }
    env.update(extra)
    _use(monkeypatch, **env)


def test_rank_uses_supplied_query_embedding():
    chunks = [{"content": "alpha beta"}, {"content": "zzzz unrelated weather"}]
    ranked = rank_chunks_with_embeddings(
        "alpha beta",
        chunks,
        [[0.0, 1.0], [1.0, 0.0]],
        mode="vector",
        top_k=2,
        dims=64,
        query_embedding=[1.0, 0.0],
    )
    assert ranked[0][0]["content"] == "zzzz unrelated weather"
    assert ranked[0][1] == pytest.approx(1.0)
    assert len(ranked) == 1


def test_openai_target_reuses_llm_settings_and_rejects_gaps(monkeypatch):
    _use(
        monkeypatch,
        PROAGENT_KB_EMBED_BACKEND="openai",
        PROAGENT_KB_EMBED_BASE_URL="",
        PROAGENT_KB_EMBED_API_KEY="",
        PROAGENT_LLM_BASE_URL="https://llm.example/v1/",
        PROAGENT_LLM_API_KEY="llm-key",
        PROAGENT_KB_EMBED_MODEL="text-embedding-3-small",
        PROAGENT_KB_EMBED_DIMS="8",
    )
    embedder = get_embedder()
    assert embedder.base_url == "https://llm.example/v1"
    assert embedder.api_key == "llm-key"
    assert embedder.model == "text-embedding-3-small"
    assert embedder.dims == 8
    assert "llm-key" not in repr(embedder)

    _use(monkeypatch, PROAGENT_LLM_API_KEY="", PROAGENT_KB_EMBED_API_KEY="  ")
    with pytest.raises(EmbeddingsConfigError, match="PROAGENT_KB_EMBED_API_KEY"):
        get_embedder()

    _use(
        monkeypatch,
        PROAGENT_KB_EMBED_API_KEY="embed-key",
        PROAGENT_LLM_BASE_URL="",
        PROAGENT_KB_EMBED_BASE_URL="",
    )
    with pytest.raises(EmbeddingsConfigError, match="PROAGENT_KB_EMBED_BASE_URL"):
        get_embedder()

    _use(
        monkeypatch,
        PROAGENT_KB_EMBED_BASE_URL="https://embed.example/v1",
        PROAGENT_KB_EMBED_MODEL="  ",
    )
    with pytest.raises(EmbeddingsConfigError, match="PROAGENT_KB_EMBED_MODEL"):
        get_embedder()


def test_openai_embedder_posts_embeddings_and_reorders(monkeypatch):
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "body": body,
            }
        )
        dims = body["dimensions"]
        data = []
        for index, _text in enumerate(body["input"]):
            vector = [0.0] * dims
            vector[0] = float(index + 1)
            data.append({"object": "embedding", "index": index, "embedding": vector})
        data.reverse()
        return httpx.Response(200, json={"data": data, "model": "text-embedding-3-small"})

    _install_transport(monkeypatch, handler)
    _enable_openai(monkeypatch)
    vectors = get_embedder().embed_texts(["one", "two"])
    assert captured[0]["url"] == "https://embed.example/v1/embeddings"
    assert captured[0]["auth"] == "Bearer test-embed-key"
    assert captured[0]["body"] == {
        "model": "text-embedding-3-small",
        "input": ["one", "two"],
        "dimensions": 4,
    }
    assert vectors == [[1.0, 0.0, 0.0, 0.0], [2.0, 0.0, 0.0, 0.0]]
    assert get_embedder().embed_texts([]) == []


def test_openai_embedder_batches(monkeypatch):
    monkeypatch.setattr("proagent_os.professional.kb.embeddings.EMBED_BATCH_SIZE", 2)
    inputs_seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        inputs_seen.append(list(body["input"]))
        batch_no = len(inputs_seen)
        data = []
        for index in range(len(body["input"])):
            data.append(
                {
                    "index": index,
                    "embedding": [float(batch_no * 10 + index), 0.0, 0.0, 0.0],
                }
            )
        data.reverse()
        return httpx.Response(200, json={"data": data})

    _install_transport(monkeypatch, handler)
    _enable_openai(monkeypatch)
    vectors = get_embedder().embed_texts(["a", "b", "c"])
    assert inputs_seen == [["a", "b"], ["c"]]
    assert [row[0] for row in vectors] == [10.0, 11.0, 20.0]


@pytest.mark.parametrize(
    "response, match",
    [
        (httpx.Response(401, json={"error": {"message": "invalid key"}}), "HTTP 401"),
        (httpx.Response(200, text="not-json"), "not JSON"),
        (
            httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}),
            "PROAGENT_KB_EMBED_DIMS=4",
        ),
        (
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                        {"index": 0, "embedding": [0.0, 1.0, 0.0, 0.0]},
                    ]
                },
            ),
            "duplicate embedding index",
        ),
        (
            httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0, True, 0.0, 0.0]}]},
            ),
            "finite numbers",
        ),
    ],
)
def test_openai_embedder_rejects_bad_responses(monkeypatch, response, match):
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    _install_transport(monkeypatch, handler)
    _enable_openai(monkeypatch)
    with pytest.raises(EmbeddingsError, match=match) as raised:
        get_embedder().embed_texts(["hello", "world"] if "duplicate" in match else ["hello"])
    assert "test-embed-key" not in str(raised.value)


def test_openai_embedder_transport_error_hides_key(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, handler)
    _enable_openai(monkeypatch)
    with pytest.raises(EmbeddingsError, match="ConnectError") as raised:
        get_embedder().embed_texts(["hello"])
    assert "test-embed-key" not in str(raised.value)


def test_stub_hybrid_does_not_open_http(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("stub backend must not open an HTTP client")

    monkeypatch.setattr("proagent_os.professional.kb.embeddings.httpx.Client", boom)
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="hybrid", PROAGENT_KB_EMBED_BACKEND="stub")
    tid = _tenant()
    kb = get_services().kb
    kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    assert kb.search(tid, "alpha beta", top_k=1)[0]["content"] == "alpha beta"
    assert kb.reindex_embeddings(tid)["model"] == "stub"


def test_bm25_skips_openai_when_unconfigured(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("bm25 must not open an HTTP client")

    monkeypatch.setattr("proagent_os.professional.kb.embeddings.httpx.Client", boom)
    _use(
        monkeypatch,
        PROAGENT_KB_RETRIEVAL="bm25",
        PROAGENT_KB_EMBED_BACKEND="openai",
        PROAGENT_KB_EMBED_API_KEY="",
        PROAGENT_LLM_API_KEY="",
    )
    tid = _tenant()
    kb = get_services().kb
    meta = kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    assert meta["chunks"] == 1
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0
    assert kb.search(tid, "alpha beta", top_k=1)[0]["content"] == "alpha beta"


def test_hybrid_openai_without_key_writes_nothing(monkeypatch):
    _use(
        monkeypatch,
        PROAGENT_KB_RETRIEVAL="hybrid",
        PROAGENT_KB_EMBED_BACKEND="openai",
        PROAGENT_KB_EMBED_API_KEY="",
        PROAGENT_LLM_API_KEY="",
    )
    tid = _tenant()
    kb = get_services().kb
    with pytest.raises(EmbeddingsConfigError, match="API key"):
        kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_documents")["n"] == 0
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunks")["n"] == 0
    with pytest.raises(EmbeddingsConfigError):
        kb.search(tid, "alpha beta")
    with pytest.raises(EmbeddingsConfigError):
        kb.reindex_embeddings(tid)


def _vector_table() -> dict[str, list[float]]:
    return {
        "alpha beta": [1.0, 0.0, 0.0, 0.0],
        "alpha alpha alpha beta": [0.5, 0.5, 0.5, 0.5],
        "zzzz unrelated weather": [0.0, 0.0, 1.0, 0.0],
    }


def _table_handler(table: dict[str, list[float]], calls: list[list[str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(list(body["input"]))
        data = []
        for index, text in enumerate(body["input"]):
            data.append({"index": index, "embedding": table[text]})
        data.reverse()
        return httpx.Response(200, json={"data": data})

    return handler


def test_openai_ingest_search_and_reindex(monkeypatch):
    table = _vector_table()
    calls: list[list[str]] = []
    _install_transport(monkeypatch, _table_handler(table, calls))
    _enable_openai(monkeypatch, PROAGENT_KB_HYBRID_ALPHA="1")
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector")
    tid = _tenant()
    kb = get_services().kb
    kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    kb.ingest_text(tid, "manual", "Repeated", "alpha alpha alpha beta")
    kb.ingest_text(tid, "manual", "Other", "zzzz unrelated weather")

    rows = get_db().fetchall(
        "SELECT dims, model, embedding FROM kb_chunk_embeddings WHERE tenant_id=? ORDER BY rowid",
        (tid,),
    )
    assert len(rows) == 3
    assert {row["model"] for row in rows} == {"text-embedding-3-small"}
    assert {row["dims"] for row in rows} == {4}
    assert unpack_embedding(rows[0]["embedding"]) == pytest.approx([1.0, 0.0, 0.0, 0.0])

    hits = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in hits] == ["alpha beta", "alpha alpha alpha beta"]
    assert [hit["score"] for hit in hits] == [1.0, 0.5]

    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="hybrid", PROAGENT_KB_HYBRID_ALPHA="1")
    hybrid = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in hybrid] == ["alpha beta", "alpha alpha alpha beta"]

    before = get_db().fetchall(
        "SELECT chunk_id, dims, model, embedding FROM kb_chunk_embeddings WHERE tenant_id=? ORDER BY chunk_id",
        (tid,),
    )
    stats = kb.reindex_embeddings(tid)
    after = get_db().fetchall(
        "SELECT chunk_id, dims, model, embedding FROM kb_chunk_embeddings WHERE tenant_id=? ORDER BY chunk_id",
        (tid,),
    )
    assert stats == {
        "chunks": 3,
        "upserted": 3,
        "dims": 4,
        "model": "text-embedding-3-small",
    }
    assert before == after
    assert calls[:4] == [
        ["alpha beta"],
        ["alpha alpha alpha beta"],
        ["zzzz unrelated weather"],
        ["alpha beta"],
    ]
    assert calls[4] == ["alpha beta"]
    assert sorted(calls[5]) == sorted(table)


def test_openai_model_change_falls_back_until_reindex(monkeypatch):
    table = _vector_table()
    calls: list[list[str]] = []
    _install_transport(monkeypatch, _table_handler(table, calls))
    _enable_openai(monkeypatch)
    tid = _tenant()
    kb = get_services().kb
    kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    assert len(calls) == 1

    _use(monkeypatch, PROAGENT_KB_EMBED_MODEL="text-embedding-3-large")
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="bm25")
    bm25_hits = kb.search(tid, "alpha beta", top_k=1)
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector")
    assert kb.search(tid, "alpha beta", top_k=1) == bm25_hits
    assert len(calls) == 1

    table["alpha beta"] = [0.0, 1.0, 0.0, 0.0]
    stats = kb.reindex_embeddings(tid)
    assert stats["model"] == "text-embedding-3-large"
    stored = get_db().fetchone(
        "SELECT model, embedding FROM kb_chunk_embeddings WHERE tenant_id=?",
        (tid,),
    )
    assert stored["model"] == "text-embedding-3-large"
    assert unpack_embedding(stored["embedding"]) == pytest.approx([0.0, 1.0, 0.0, 0.0])
    hits = kb.search(tid, "alpha beta", top_k=1)
    assert hits[0]["content"] == "alpha beta"
    assert hits[0]["score"] == 1.0


def test_openai_width_mismatch_does_not_insert(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    _install_transport(monkeypatch, handler)
    _enable_openai(monkeypatch, PROAGENT_KB_RETRIEVAL="hybrid")
    tid = _tenant()
    with pytest.raises(EmbeddingsError, match="width"):
        get_services().kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_documents")["n"] == 0


def test_kb_api_reports_missing_embed_key(monkeypatch):
    _use(
        monkeypatch,
        PROAGENT_KB_RETRIEVAL="hybrid",
        PROAGENT_KB_EMBED_BACKEND="openai",
        PROAGENT_KB_EMBED_API_KEY="",
        PROAGENT_LLM_API_KEY="",
    )
    from proagent_os.main import create_app

    with TestClient(create_app()) as client:
        created = client.post(
            "/api/v1/tenants",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"name": "Embed Co", "tone": "concise"},
        )
        assert created.status_code == 200, created.text
        token = {"Authorization": f"Bearer {created.json()['api_key']}"}
        denied = client.post(
            "/api/v1/kb/ingest/text",
            headers=token,
            json={"title": "Exact", "content": "alpha beta"},
        )
        assert denied.status_code == 503
        assert "PROAGENT_KB_EMBED_API_KEY" in denied.json()["detail"]
        assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_documents")["n"] == 0

        _use(monkeypatch, PROAGENT_KB_RETRIEVAL="bm25")
        allowed = client.post(
            "/api/v1/kb/ingest/text",
            headers=token,
            json={"title": "Exact", "content": "alpha beta"},
        )
        assert allowed.status_code == 200, allowed.text
        found = client.post(
            "/api/v1/kb/search",
            headers=token,
            json={"query": "alpha beta", "top_k": 1},
        )
        assert found.status_code == 200
        assert found.json()["hits"][0]["content"] == "alpha beta"
