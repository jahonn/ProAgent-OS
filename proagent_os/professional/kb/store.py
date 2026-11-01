from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from proagent_os.config import get_settings
from proagent_os.db import get_db, new_id, utcnow
from proagent_os.professional.kb.chunking import chunk_text
from proagent_os.professional.kb.embeddings import EmbeddingsError, get_embedder
from proagent_os.professional.kb.ingest import fetch_url_text, read_local_file
from proagent_os.professional.kb.retrieve import (
    rank_chunks,
    rank_chunks_with_embeddings,
    rank_chunks_with_similarities,
    unpack_embedding,
)
from proagent_os.professional.kb.vector_store import get_vector_store


class KnowledgeBase:
    def __init__(self) -> None:
        self.db = get_db()

    def ingest_text(self, tenant_id: str, source: str, title: str, content: str) -> Dict[str, Any]:
        chunks = chunk_text(content)
        # Embed before any insert so a failed request leaves the corpus unchanged.
        # BM25 does not construct an embedder, including when the backend is openai.
        vectors = None
        model = ""
        dims = 0
        settings = get_settings()
        if settings.kb_retrieval != "bm25" and chunks:
            embedder = get_embedder()
            # A missing sqlite-vec extension fails here, before any insert and
            # before a remote embeddings call. BM25 does not enter this branch.
            get_vector_store(settings).ensure_available(self.db)
            vectors = embedder.embed_texts(chunks)
            if len(vectors) != len(chunks):
                raise EmbeddingsError(
                    f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
                )
            model = embedder.model
            dims = embedder.dims
        doc_id = new_id("doc")
        self.db.insert(
            "kb_documents",
            {
                "id": doc_id,
                "tenant_id": tenant_id,
                "source": source,
                "title": title or source,
                "content": content,
                "created_at": utcnow(),
            },
        )
        for i, ch in enumerate(chunks):
            chunk_id = new_id("chk")
            self.db.insert(
                "kb_chunks",
                {
                    "id": chunk_id,
                    "document_id": doc_id,
                    "tenant_id": tenant_id,
                    "chunk_index": i,
                    "content": ch,
                    "tokens_estimate": max(1, len(ch.split())),
                },
            )
            if vectors is not None:
                self._write_embedding(chunk_id, tenant_id, vectors[i], dims=dims, model=model)
        return {"document_id": doc_id, "title": title, "chunks": len(chunks), "source": source}

    async def ingest_url(self, tenant_id: str, url: str) -> Dict[str, Any]:
        title, text = await fetch_url_text(url)
        return self.ingest_text(tenant_id, source=url, title=title, content=text)

    def ingest_file(self, tenant_id: str, path: Path) -> Dict[str, Any]:
        title, text = read_local_file(path)
        return self.ingest_text(tenant_id, source=str(path), title=title, content=text)

    def list_documents(self, tenant_id: str) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            "SELECT id, source, title, created_at, length(content) AS chars FROM kb_documents WHERE tenant_id=? ORDER BY created_at DESC",
            (tenant_id,),
        )

    def search(self, tenant_id: str, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        chunks = self._load_chunks(tenant_id)
        settings = get_settings()
        if settings.kb_retrieval == "bm25":
            ranked = rank_chunks(query, chunks, top_k=top_k)
        else:
            ranked = self._rank_embedded(tenant_id, query, chunks, top_k=top_k)
        return [_hit(c, score) for c, score in ranked]

    def reindex_embeddings(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Write embeddings for existing chunks. Safe to run more than once."""
        if tenant_id:
            chunks = self.db.fetchall(
                "SELECT id, tenant_id, content FROM kb_chunks WHERE tenant_id=?",
                (tenant_id,),
            )
        else:
            chunks = self.db.fetchall("SELECT id, tenant_id, content FROM kb_chunks")
        embedder = get_embedder()
        get_vector_store().ensure_available(self.db)
        vectors = embedder.embed_texts([ch["content"] for ch in chunks]) if chunks else []
        if len(vectors) != len(chunks):
            raise EmbeddingsError(
                f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
            )
        for ch, vec in zip(chunks, vectors, strict=True):
            self._write_embedding(
                ch["id"],
                ch["tenant_id"],
                vec,
                dims=embedder.dims,
                model=embedder.model,
            )
        return {
            "chunks": len(chunks),
            "upserted": len(chunks),
            "dims": embedder.dims,
            "model": embedder.model,
        }

    def _load_chunks(self, tenant_id: str) -> List[Dict[str, Any]]:
        return self.db.fetchall(
            "SELECT c.id, c.content, c.document_id, c.chunk_index, d.title, d.source "
            "FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id "
            "WHERE c.tenant_id=?",
            (tenant_id,),
        )

    def _rank_embedded(
        self,
        tenant_id: str,
        query: str,
        chunks: Sequence[Dict[str, Any]],
        *,
        top_k: int,
    ) -> List[Tuple[Dict[str, Any], float]]:
        """Hybrid or vector rank. Incomplete embeddings fall back to BM25.

        A configured openai backend is resolved first, so a missing key or
        base URL raises instead of searching with the stub. The blob store
        still loads every embedding and scores cosine in Python. Any other
        store (sqlite-vec, or a test double) must already have loaded; a
        missing extension raises and does not fall back to blob cosine.
        """
        embedder = get_embedder()
        if not chunks:
            return []
        settings = get_settings()
        store = get_vector_store(settings)
        if store.kind == "blob":
            vectors = self._aligned_vectors(
                tenant_id,
                chunks,
                dims=embedder.dims,
                model=embedder.model,
            )
            if vectors is None:
                return rank_chunks(query, chunks, top_k=top_k)
            query_vec = embedder.embed_texts([query])[0]
            return rank_chunks_with_embeddings(
                query,
                chunks,
                vectors,
                mode=settings.kb_retrieval,
                alpha=float(settings.kb_hybrid_alpha),
                top_k=top_k,
                dims=embedder.dims,
                query_embedding=query_vec,
            )
        store.ensure_available(self.db)
        chunk_ids = [chunk["id"] for chunk in chunks]
        if not store.covers(
            self.db,
            tenant_id,
            chunk_ids,
            dims=embedder.dims,
            model=embedder.model,
        ):
            return rank_chunks(query, chunks, top_k=top_k)
        query_vec = embedder.embed_texts([query])[0]
        similarities = store.similarities(
            self.db,
            tenant_id,
            chunk_ids,
            query_vec,
            dims=embedder.dims,
            model=embedder.model,
            mode=settings.kb_retrieval,
            top_k=top_k,
        )
        return rank_chunks_with_similarities(
            query,
            chunks,
            similarities,
            mode=settings.kb_retrieval,
            alpha=float(settings.kb_hybrid_alpha),
            top_k=top_k,
        )

    def _aligned_vectors(
        self,
        tenant_id: str,
        chunks: Sequence[Dict[str, Any]],
        *,
        dims: int,
        model: str,
    ) -> Optional[List[List[float]]]:
        rows = self.db.fetchall(
            "SELECT chunk_id, dims, embedding, model FROM kb_chunk_embeddings WHERE tenant_id=?",
            (tenant_id,),
        )
        by_id = {row["chunk_id"]: row for row in rows}
        vectors: List[List[float]] = []
        width = dims * 4
        for chunk in chunks:
            row = by_id.get(chunk["id"])
            if not row or int(row["dims"]) != dims or (row.get("model") or "") != model:
                return None
            blob = row["embedding"]
            if not isinstance(blob, (bytes, bytearray, memoryview)) or len(blob) != width:
                return None
            try:
                vec = unpack_embedding(blob)
            except (ValueError, struct.error):
                return None
            if len(vec) != dims:
                return None
            vectors.append(vec)
        return vectors

    def _write_embedding(
        self,
        chunk_id: str,
        tenant_id: str,
        vec: Sequence[float],
        *,
        dims: int,
        model: str,
    ) -> None:
        if len(vec) != dims:
            raise EmbeddingsError(
                f"embedding width {len(vec)} does not match PROAGENT_KB_EMBED_DIMS={dims}"
            )
        get_vector_store().upsert(
            self.db,
            chunk_id,
            tenant_id,
            vec,
            dims=dims,
            model=model,
        )


def _hit(chunk: Dict[str, Any], score: float) -> Dict[str, Any]:
    return {
        "chunk_id": chunk["id"],
        "document_id": chunk["document_id"],
        "title": chunk["title"],
        "source": chunk["source"],
        "chunk_index": chunk["chunk_index"],
        "score": round(score, 4),
        "content": chunk["content"][:1000],
    }
