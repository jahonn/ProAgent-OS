from __future__ import annotations

import pytest
from pydantic import ValidationError

from proagent_os.config import get_settings
from proagent_os.db import get_db, new_id, utcnow
from proagent_os.professional.kb.retrieve import (
    bm25_scores,
    cosine_similarity,
    embedding_stub,
    hybrid_scores,
    pack_embedding,
    tokenize,
    unpack_embedding,
)
from proagent_os.services import get_services


def test_tokenize_and_bm25():
    docs = [
        "conversion rate dropped below target",
        "unrelated weather forecast sunny",
        "improve activation to lift conversion",
    ]
    scores = bm25_scores("conversion activation", docs)
    assert scores[0] > scores[1]
    assert scores[2] > scores[1]
    assert "conversion" in tokenize("Conversion-Rate_v2")


@pytest.mark.asyncio
async def test_kb_ingest_and_search():
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "KB Co",
            "api_key": "pk_kb",
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
        },
    )
    kb = get_services().kb
    meta = await kb.ingest_url(tid, "fixture://sample_product.html")
    assert meta["chunks"] >= 1
    hits = kb.search(tid, "conversion rate target", top_k=3)
    assert hits
    assert hits[0]["score"] > 0
    assert "conversion" in hits[0]["content"].lower() or "target" in hits[0]["content"].lower()
    stored = get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")
    assert stored["n"] == 0


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


def _use_retrieval(monkeypatch: pytest.MonkeyPatch, mode: str, **extra: str) -> None:
    monkeypatch.setenv("PROAGENT_KB_RETRIEVAL", mode)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _ingest_alpha_corpus(kb, tenant_id: str) -> None:
    # BM25 prefers the repeated line; stub cosine prefers the exact query line.
    kb.ingest_text(tenant_id, "manual", "Exact", "alpha beta")
    kb.ingest_text(tenant_id, "manual", "Repeated", "alpha alpha alpha beta")
    kb.ingest_text(tenant_id, "manual", "Other", "zzzz unrelated weather")


def test_embedding_stub_is_deterministic():
    first = embedding_stub("conversion rate", dims=64)
    assert first == embedding_stub("conversion rate", dims=64)
    assert len(first) == 64
    assert sum(v * v for v in first) == pytest.approx(1.0)
    assert embedding_stub("other text", dims=64) != first
    assert embedding_stub("", dims=4) == [0.0, 0.0, 0.0, 0.0]
    nonzero = [(i, round(v, 6)) for i, v in enumerate(first) if abs(v) > 1e-9]
    assert nonzero == [(5, 0.632456), (11, 0.632456), (12, 0.316228), (39, 0.316228)]
    assert unpack_embedding(pack_embedding(first)) == pytest.approx(first, abs=1e-6)
    assert cosine_similarity(first, first) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
    with pytest.raises(ValueError):
        embedding_stub("x", dims=0)


def test_embedding_stub_defaults_to_configured_dims(monkeypatch):
    monkeypatch.setenv("PROAGENT_KB_EMBED_DIMS", "16")
    get_settings.cache_clear()
    assert len(embedding_stub("hello")) == 16
    assert len(embedding_stub("hello", dims=8)) == 8


def test_hybrid_scores_mix_normalized_signals():
    bm25 = [1.0, 4.0, 0.0]
    vector = [2.0, 1.0, -0.2]
    # max-norm bm25 = [0.25, 1, 0]; max-norm vector = [1, 0.5, 0]
    assert hybrid_scores(bm25, vector, alpha=0.5) == pytest.approx([0.625, 0.75, 0.0])
    assert hybrid_scores(bm25, vector, alpha=0.0) == pytest.approx([0.25, 1.0, 0.0])
    assert hybrid_scores(bm25, vector, alpha=1.0) == pytest.approx([1.0, 0.5, 0.0])
    with pytest.raises(ValueError):
        hybrid_scores([1.0], [1.0, 2.0])


def test_bm25_search_ignores_missing_and_present_embeddings():
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha_corpus(kb, tid)
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0
    missing = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in missing] == ["alpha alpha alpha beta", "alpha beta"]
    assert missing[0]["score"] == 1.1318
    assert missing[1]["score"] == 1.1059
    assert list(missing[0]) == [
        "chunk_id",
        "document_id",
        "title",
        "source",
        "chunk_index",
        "score",
        "content",
    ]
    kb.reindex_embeddings(tid)
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 3
    assert kb.search(tid, "alpha beta", top_k=3) == missing


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_empty_embedding_table_falls_back_to_bm25(monkeypatch, mode):
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha_corpus(kb, tid)
    bm25_hits = kb.search(tid, "alpha beta", top_k=3)
    _use_retrieval(monkeypatch, mode, PROAGENT_KB_HYBRID_ALPHA="1")
    assert kb.search(tid, "alpha beta", top_k=3) == bm25_hits
    assert get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunk_embeddings")["n"] == 0


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_partial_or_corrupt_embeddings_fall_back_to_bm25(monkeypatch, mode):
    tid = _tenant()
    kb = get_services().kb
    _use_retrieval(monkeypatch, mode, PROAGENT_KB_HYBRID_ALPHA="0.5")
    _ingest_alpha_corpus(kb, tid)
    row = get_db().fetchone(
        "SELECT chunk_id FROM kb_chunk_embeddings WHERE tenant_id=? LIMIT 1",
        (tid,),
    )
    get_db().execute("DELETE FROM kb_chunk_embeddings WHERE chunk_id=?", (row["chunk_id"],))
    partial = kb.search(tid, "alpha beta", top_k=3)

    get_db().execute(
        "UPDATE kb_chunk_embeddings SET embedding=? WHERE tenant_id=?",
        (b"not-a-vector", tid),
    )
    corrupt = kb.search(tid, "alpha beta", top_k=3)

    _use_retrieval(monkeypatch, "bm25")
    bm25_hits = kb.search(tid, "alpha beta", top_k=3)
    assert partial == bm25_hits
    assert corrupt == bm25_hits


def test_hybrid_alpha_reorders_against_bm25(monkeypatch):
    tid = _tenant()
    kb = get_services().kb
    _use_retrieval(monkeypatch, "hybrid", PROAGENT_KB_HYBRID_ALPHA="0")
    _ingest_alpha_corpus(kb, tid)
    chunks = get_db().fetchone("SELECT COUNT(*) AS n FROM kb_chunks WHERE tenant_id=?", (tid,))
    embeds = get_db().fetchone(
        "SELECT COUNT(*) AS n FROM kb_chunk_embeddings WHERE tenant_id=?",
        (tid,),
    )
    assert embeds["n"] == chunks["n"] == 3

    lexical = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in lexical] == ["alpha alpha alpha beta", "alpha beta"]
    assert [hit["score"] for hit in lexical] == [1.0, 0.9771]

    _use_retrieval(monkeypatch, "hybrid", PROAGENT_KB_HYBRID_ALPHA="1")
    semantic = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in semantic] == ["alpha beta", "alpha alpha alpha beta"]
    assert [hit["score"] for hit in semantic] == [1.0, 0.9222]

    _use_retrieval(monkeypatch, "vector")
    pure = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["content"] for hit in pure] == ["alpha beta", "alpha alpha alpha beta"]
    assert pure[0]["score"] == 1.0


def test_reindex_embeddings_is_idempotent():
    tid = _tenant()
    kb = get_services().kb
    _ingest_alpha_corpus(kb, tid)
    first = kb.reindex_embeddings(tid)
    rows_sql = (
        "SELECT chunk_id, tenant_id, dims, embedding, model "
        "FROM kb_chunk_embeddings WHERE tenant_id=? ORDER BY chunk_id"
    )
    rows_first = get_db().fetchall(rows_sql, (tid,))
    second = kb.reindex_embeddings(tid)
    rows_second = get_db().fetchall(rows_sql, (tid,))
    assert first == second == {"chunks": 3, "upserted": 3, "dims": 64, "model": "stub"}
    assert rows_first == rows_second
    assert len({row["chunk_id"] for row in rows_first}) == 3
    assert all(row["model"] == "stub" and row["dims"] == 64 for row in rows_first)

    again = kb.reindex_embeddings()
    assert again["chunks"] == 3
    assert get_db().fetchall(rows_sql, (tid,)) == rows_first


def test_dim_mismatch_falls_back_until_reindex(monkeypatch):
    tid = _tenant()
    kb = get_services().kb
    _use_retrieval(monkeypatch, "hybrid", PROAGENT_KB_HYBRID_ALPHA="1")
    _ingest_alpha_corpus(kb, tid)
    assert kb.search(tid, "alpha beta", top_k=1)[0]["content"] == "alpha beta"

    _use_retrieval(monkeypatch, "hybrid", PROAGENT_KB_EMBED_DIMS="32", PROAGENT_KB_HYBRID_ALPHA="1")
    fallback = kb.search(tid, "alpha beta", top_k=3)
    assert [hit["score"] for hit in fallback] == [1.1318, 1.1059]

    stats = kb.reindex_embeddings(tid)
    assert stats == {"chunks": 3, "upserted": 3, "dims": 32, "model": "stub"}
    refreshed = kb.search(tid, "alpha beta", top_k=1)
    assert refreshed[0]["content"] == "alpha beta"
    dims = {
        row["dims"]
        for row in get_db().fetchall(
            "SELECT dims FROM kb_chunk_embeddings WHERE tenant_id=?",
            (tid,),
        )
    }
    assert dims == {32}


def test_embedding_model_mismatch_falls_back_to_bm25(monkeypatch):
    tid = _tenant()
    kb = get_services().kb
    _use_retrieval(monkeypatch, "vector")
    _ingest_alpha_corpus(kb, tid)
    assert kb.search(tid, "alpha beta", top_k=1)[0]["content"] == "alpha beta"
    get_db().execute(
        "UPDATE kb_chunk_embeddings SET model=? WHERE tenant_id=?",
        ("other-model", tid),
    )
    _use_retrieval(monkeypatch, "bm25")
    bm25_hits = kb.search(tid, "alpha beta", top_k=3)
    _use_retrieval(monkeypatch, "vector")
    assert kb.search(tid, "alpha beta", top_k=3) == bm25_hits


def test_retrieval_settings_defaults_and_validation(monkeypatch):
    settings = get_settings()
    assert settings.kb_retrieval == "bm25"
    assert settings.kb_embed_backend == "stub"
    assert settings.kb_embed_dims == 64
    assert settings.kb_hybrid_alpha == 0.5
    assert settings.kb_embed_model == "text-embedding-3-small"
    assert settings.kb_embed_base_url == ""
    assert settings.kb_embed_api_key == ""

    monkeypatch.setenv("PROAGENT_KB_RETRIEVAL", "Hybrid")
    get_settings.cache_clear()
    assert get_settings().kb_retrieval == "hybrid"

    monkeypatch.setenv("PROAGENT_KB_EMBED_BACKEND", "OpenAI")
    get_settings.cache_clear()
    assert get_settings().kb_embed_backend == "openai"

    monkeypatch.setenv("PROAGENT_KB_EMBED_BACKEND", "cohere")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()

    monkeypatch.setenv("PROAGENT_KB_EMBED_BACKEND", "stub")
    monkeypatch.setenv("PROAGENT_KB_HYBRID_ALPHA", "1.5")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
