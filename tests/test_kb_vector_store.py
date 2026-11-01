"""Optional sqlite-vec index. Default tests never import the extension.

The scripted store covers wiring on CI. Tests marked ``sqlite_vec`` run only
when the extension loads; otherwise they skip.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

from proagent_os.config import get_settings
from proagent_os.db import get_db, new_id, utcnow
from proagent_os.professional.kb.retrieve import cosine_similarity, embedding_stub, rank_chunks_with_similarities
from proagent_os.professional.kb.vector_store import (
    SQLITE_VEC_K_MAX,
    VectorStoreConfigError,
    get_vector_store,
    knn_k,
    load_sqlite_vec_extension,
    vec_table_name,
)
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


def _ingest_alpha(kb, tenant_id: str) -> None:
    kb.ingest_text(tenant_id, "manual", "Exact", "alpha beta")
    kb.ingest_text(tenant_id, "manual", "Repeated", "alpha alpha alpha beta")
    kb.ingest_text(tenant_id, "manual", "Other", "zzzz unrelated weather")


class ScriptedIndex:
    """In-memory stand-in for the sqlite-vec store. No native extension."""

    kind = "scripted"

    def __init__(self, scores: list[float] | None = None) -> None:
        self.scores = scores
        self.complete = True
        self.upserts: list[dict] = []
        self.ensure_calls = 0
        self.similarity_calls = 0

    def ensure_available(self, db) -> None:
        self.ensure_calls += 1

    def upsert(self, db, chunk_id, tenant_id, vec, *, dims, model) -> None:
        self.upserts.append(
            {
                "chunk_id": chunk_id,
                "tenant_id": tenant_id,
                "dims": dims,
                "model": model,
                "width": len(vec),
            }
        )

    def covers(self, db, tenant_id, chunk_ids, *, dims, model) -> bool:
        return self.complete

    def similarities(self, db, tenant_id, chunk_ids, query_vec, *, dims, model, mode, top_k):
        self.similarity_calls += 1
        assert self.scores is not None
        assert len(self.scores) == len(self.upserts)
        by_id = {
            row["chunk_id"]: score
            for row, score in zip(self.upserts, self.scores, strict=True)
        }
        return [by_id[chunk_id] for chunk_id in chunk_ids]


def test_knn_k_and_table_name_are_bounded():
    assert knn_k(10, mode="vector", top_k=5) == 5
    assert knn_k(3, mode="vector", top_k=5) == 3
    assert knn_k(10, mode="hybrid", top_k=5) == 10
    assert knn_k(SQLITE_VEC_K_MAX + 100, mode="hybrid", top_k=5) == SQLITE_VEC_K_MAX
    assert knn_k(SQLITE_VEC_K_MAX + 100, mode="vector", top_k=8) == 8
    assert knn_k(4, mode="vector", top_k=0) == 0
    assert knn_k(0, mode="hybrid", top_k=5) == 0
    assert vec_table_name(64) == "kb_vec_d64"
    assert vec_table_name(4096) == "kb_vec_d4096"
    with pytest.raises(Exception):
        vec_table_name(0)
    with pytest.raises(Exception):
        vec_table_name(4097)


def test_rank_chunks_with_similarities_fuses_like_cosine():
    chunks = [{"content": "alpha beta"}, {"content": "other"}]
    ranked = rank_chunks_with_similarities(
        "alpha beta",
        chunks,
        [0.2, 0.8],
        mode="vector",
        top_k=2,
    )
    assert ranked[0][0]["content"] == "other"
    assert ranked[0][1] == pytest.approx(0.8)
    hybrid = rank_chunks_with_similarities(
        "alpha beta",
        chunks,
        [0.0, 0.0],
        mode="bm25",
        top_k=1,
    )
    assert hybrid[0][0]["content"] == "alpha beta"


def test_default_bm25_does_not_import_sqlite_vec():
    sys.modules.pop("sqlite_vec", None)
    assert get_vector_store().kind == "blob"
    assert get_settings().kb_vector_store == "blob"
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha(kb, tid)
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0
    bm25 = kb.search(tid, "alpha beta", top_k=2)
    assert [hit["content"] for hit in bm25] == ["alpha alpha alpha beta", "alpha beta"]
    assert (
        get_db().fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_vec_index'"
        )
        is None
    )
    assert "sqlite_vec" not in sys.modules


def test_default_blob_vector_search_writes_blobs(monkeypatch):
    sys.modules.pop("sqlite_vec", None)
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector", PROAGENT_KB_VECTOR_STORE="blob")
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha(kb, tid)
    hits = kb.search(tid, "alpha beta", top_k=2)
    assert [hit["content"] for hit in hits] == ["alpha beta", "alpha alpha alpha beta"]
    assert hits[0]["score"] == 1.0
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 3
    assert (
        get_db().fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_vec_index'"
        )
        is None
    )
    assert "sqlite_vec" not in sys.modules


def test_factory_sqlite_vec_does_not_import_until_load(monkeypatch):
    sys.modules.pop("sqlite_vec", None)
    _use(monkeypatch, PROAGENT_KB_VECTOR_STORE="SQLite_Vec")
    store = get_vector_store()
    assert store.kind == "sqlite_vec"
    assert get_settings().kb_vector_store == "sqlite_vec"
    assert "sqlite_vec" not in sys.modules

    monkeypatch.setenv("PROAGENT_KB_VECTOR_STORE", "faiss")
    get_settings.cache_clear()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        get_settings()


def test_bm25_does_not_load_extension_when_sqlite_vec_is_selected(monkeypatch):
    sys.modules.pop("sqlite_vec", None)
    loaded: list[str] = []

    def boom(conn):
        loaded.append("load")
        raise VectorStoreConfigError("should not load")

    monkeypatch.setattr(
        "proagent_os.professional.kb.vector_store.load_sqlite_vec_extension",
        boom,
    )
    _use(
        monkeypatch,
        PROAGENT_KB_VECTOR_STORE="sqlite_vec",
        PROAGENT_KB_RETRIEVAL="bm25",
    )
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha(kb, tid)
    hits = kb.search(tid, "alpha beta", top_k=2)
    assert [hit["content"] for hit in hits] == ["alpha alpha alpha beta", "alpha beta"]
    assert loaded == []
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0
    assert "sqlite_vec" not in sys.modules


def test_missing_package_raises_before_insert(monkeypatch):
    def missing(_conn):
        raise VectorStoreConfigError(
            "PROAGENT_KB_VECTOR_STORE=sqlite_vec requires the sqlite-vec package. "
            "Install it with: pip install 'proagent-os[vec]'."
        )

    monkeypatch.setattr(
        "proagent_os.professional.kb.vector_store.load_sqlite_vec_extension",
        missing,
    )
    _use(
        monkeypatch,
        PROAGENT_KB_VECTOR_STORE="sqlite_vec",
        PROAGENT_KB_RETRIEVAL="hybrid",
    )
    tid = _tenant()
    kb = get_services().kb
    with pytest.raises(VectorStoreConfigError, match="proagent-os\\[vec\\]"):
        kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_documents")["n"] == 0
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0

    # BM25 still ingests and searches without the extension.
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="bm25")
    kb.ingest_text(tid, "manual", "Exact", "alpha beta")
    assert kb.search(tid, "alpha beta", top_k=1)[0]["content"] == "alpha beta"

    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector")
    with pytest.raises(VectorStoreConfigError, match="sqlite-vec"):
        kb.search(tid, "alpha beta", top_k=1)


def test_loader_reports_import_and_load_failures(monkeypatch):
    def missing_import():
        raise ImportError("No module named 'sqlite_vec'")

    monkeypatch.setattr(
        "proagent_os.professional.kb.vector_store._import_sqlite_vec",
        missing_import,
    )
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(VectorStoreConfigError, match="pip install 'proagent-os\\[vec\\]'"):
            load_sqlite_vec_extension(conn)
    finally:
        conn.close()

    class Broken:
        @staticmethod
        def load(connection):
            raise sqlite3.OperationalError("not authorized")

    monkeypatch.setattr(
        "proagent_os.professional.kb.vector_store._import_sqlite_vec",
        lambda: Broken,
    )
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(VectorStoreConfigError, match="could not load the sqlite-vec extension"):
            load_sqlite_vec_extension(conn)
    finally:
        conn.close()


def test_scripted_index_scores_drive_search_and_skip_blobs(monkeypatch):
    scripted = ScriptedIndex(scores=[0.1, 0.1, 1.0])
    monkeypatch.setattr(
        "proagent_os.professional.kb.store.get_vector_store",
        lambda settings=None: scripted,
    )
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector", PROAGENT_KB_HYBRID_ALPHA="1")
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha(kb, tid)
    assert scripted.ensure_calls == 3
    assert len(scripted.upserts) == 3
    assert {row["model"] for row in scripted.upserts} == {"stub"}
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0

    hits = kb.search(tid, "alpha beta", top_k=3)
    assert scripted.similarity_calls == 1
    assert [hit["content"] for hit in hits] == [
        "zzzz unrelated weather",
        "alpha beta",
        "alpha alpha alpha beta",
    ]
    assert hits[0]["score"] == 1.0

    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="hybrid", PROAGENT_KB_HYBRID_ALPHA="0")
    lexical = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in lexical] == ["alpha alpha alpha beta", "alpha beta"]

    scripted.complete = False
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector")
    calls_before = scripted.similarity_calls
    fallback = kb.search(tid, "alpha beta", top_k=3)
    assert scripted.similarity_calls == calls_before
    assert [hit["content"] for hit in fallback] == ["alpha alpha alpha beta", "alpha beta"]


def test_api_missing_extension_is_503_and_bm25_still_searches(monkeypatch):
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="bm25", PROAGENT_KB_VECTOR_STORE="blob")
    from proagent_os.main import create_app

    def missing(_conn):
        raise VectorStoreConfigError(
            "PROAGENT_KB_VECTOR_STORE=sqlite_vec requires the sqlite-vec package. "
            "Install it with: pip install 'proagent-os[vec]'."
        )

    monkeypatch.setattr(
        "proagent_os.professional.kb.vector_store.load_sqlite_vec_extension",
        missing,
    )
    with TestClient(create_app()) as client:
        created = client.post(
            "/api/v1/tenants",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"name": "Vec Co", "tone": "concise"},
        )
        assert created.status_code == 200, created.text
        token = {"Authorization": f"Bearer {created.json()['api_key']}"}
        ingested = client.post(
            "/api/v1/kb/ingest/text",
            headers=token,
            json={"title": "Exact", "content": "alpha beta"},
        )
        assert ingested.status_code == 200, ingested.text

        _use(
            monkeypatch,
            PROAGENT_KB_RETRIEVAL="vector",
            PROAGENT_KB_VECTOR_STORE="sqlite_vec",
        )
        denied = client.post(
            "/api/v1/kb/search",
            headers=token,
            json={"query": "alpha beta", "top_k": 1},
        )
        assert denied.status_code == 503
        assert "sqlite-vec" in denied.json()["detail"]
        assert "proagent-os[vec]" in denied.json()["detail"]

        blocked = client.post(
            "/api/v1/kb/ingest/text",
            headers=token,
            json={"title": "More", "content": "more alpha"},
        )
        assert blocked.status_code == 503
        assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_documents")["n"] == 1

        _use(monkeypatch, PROAGENT_KB_RETRIEVAL="bm25")
        found = client.post(
            "/api/v1/kb/search",
            headers=token,
            json={"query": "alpha beta", "top_k": 1},
        )
        assert found.status_code == 200
        assert found.json()["hits"][0]["content"] == "alpha beta"


def _extension_loads() -> bool:
    try:
        import sqlite_vec
    except ImportError:
        return False
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except Exception:
        return False
    finally:
        conn.close()


@pytest.fixture
def sqlite_vec_extension():
    pytest.importorskip(
        "sqlite_vec",
        reason="sqlite-vec extra is not installed; pip install 'proagent-os[vec]'",
    )
    if not _extension_loads():
        pytest.skip("sqlite-vec is installed but the extension failed to load")


@pytest.mark.sqlite_vec
def test_sqlite_vec_knn_orders_like_stub_cosine(monkeypatch, sqlite_vec_extension):
    _use(
        monkeypatch,
        PROAGENT_KB_VECTOR_STORE="sqlite_vec",
        PROAGENT_KB_RETRIEVAL="vector",
        PROAGENT_KB_HYBRID_ALPHA="1",
    )
    tid = _tenant()
    other = _tenant()
    kb = get_services().kb
    _ingest_alpha(kb, tid)
    kb.ingest_text(other, "manual", "Gamma", "gamma only")
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0
    registered = get_db().fetchone("SELECT dims, distance_metric FROM kb_vec_index")
    assert dict(registered) == {"dims": 64, "distance_metric": "cosine"}

    hits = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in hits] == ["alpha beta", "alpha alpha alpha beta"]
    assert hits[0]["score"] == 1.0
    expected_second = cosine_similarity(
        embedding_stub("alpha beta", dims=64),
        embedding_stub("alpha alpha alpha beta", dims=64),
    )
    assert hits[1]["score"] == pytest.approx(round(expected_second, 4), abs=1e-3)

    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="hybrid", PROAGENT_KB_HYBRID_ALPHA="1")
    hybrid = kb.search(tid, "alpha beta", top_k=2)
    assert [hit["content"] for hit in hybrid] == ["alpha beta", "alpha alpha alpha beta"]

    isolated = kb.search(other, "gamma only", top_k=5)
    assert [hit["content"] for hit in isolated] == ["gamma only"]

    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="bm25")
    bm25_hits = kb.search(tid, "alpha beta", top_k=3)
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector")
    with get_db().connect(initializer=load_sqlite_vec_extension) as conn:
        row = conn.execute(
            "SELECT chunk_id FROM kb_vec_d64 WHERE tenant_id = ? LIMIT 1",
            (tid,),
        ).fetchone()
        conn.execute("DELETE FROM kb_vec_d64 WHERE chunk_id = ?", (row["chunk_id"],))
    assert kb.search(tid, "alpha beta", top_k=3) == bm25_hits


@pytest.mark.sqlite_vec
def test_sqlite_vec_model_and_dim_mismatch_fall_back(monkeypatch, sqlite_vec_extension):
    _use(
        monkeypatch,
        PROAGENT_KB_VECTOR_STORE="sqlite_vec",
        PROAGENT_KB_RETRIEVAL="vector",
    )
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha(kb, tid)
    assert kb.search(tid, "alpha beta", top_k=1)[0]["content"] == "alpha beta"

    with get_db().connect(initializer=load_sqlite_vec_extension) as conn:
        row = conn.execute(
            "SELECT chunk_id, embedding FROM kb_vec_d64 WHERE tenant_id = ? LIMIT 1",
            (tid,),
        ).fetchone()
        conn.execute("DELETE FROM kb_vec_d64 WHERE chunk_id = ?", (row["chunk_id"],))
        conn.execute(
            "INSERT INTO kb_vec_d64 (chunk_id, tenant_id, model, embedding) VALUES (?, ?, ?, ?)",
            (row["chunk_id"], tid, "other-model", row["embedding"]),
        )
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="bm25")
    bm25_hits = kb.search(tid, "alpha beta", top_k=3)
    _use(monkeypatch, PROAGENT_KB_RETRIEVAL="vector")
    assert kb.search(tid, "alpha beta", top_k=3) == bm25_hits

    _use(monkeypatch, PROAGENT_KB_EMBED_DIMS="32")
    narrowed = kb.search(tid, "alpha beta", top_k=3)
    assert narrowed == bm25_hits
    stats = kb.reindex_embeddings(tid)
    assert stats == {"chunks": 3, "upserted": 3, "dims": 32, "model": "stub"}
    assert kb.search(tid, "alpha beta", top_k=1)[0]["content"] == "alpha beta"
    again = kb.reindex_embeddings(tid)
    assert again == stats
    with get_db().connect(initializer=load_sqlite_vec_extension) as conn:
        stale = conn.execute(
            "SELECT COUNT(*) AS n FROM kb_vec_d64 WHERE tenant_id = ?",
            (tid,),
        ).fetchone()["n"]
        current = conn.execute(
            "SELECT COUNT(*) AS n FROM kb_vec_d32 WHERE tenant_id = ?",
            (tid,),
        ).fetchone()["n"]
    assert stale == 0
    assert current == 3
