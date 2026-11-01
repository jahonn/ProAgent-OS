"""Where hybrid and vector search store chunk embeddings.

``blob`` (the default) writes float32 BLOBs to ``kb_chunk_embeddings`` and
scores them with cosine in Python. ``sqlite_vec`` writes a sqlite-vec ``vec0``
table and asks that extension for cosine distance. The package is imported
only when a connection is actually loaded, so BM25 and the default store never
import sqlite-vec.

sqlite-vec 0.1 ``vec0`` KNN is exhaustive inside the extension (not HNSW) and
rejects ``k`` above 4096. Vector mode asks for ``top_k`` neighbors. Hybrid mode
asks for every chunk so the fusion matches the blob store; above the cap, chunks
outside the nearest 4096 contribute a vector score of 0.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence

from proagent_os.config import Settings, get_settings
from proagent_os.db import Database
from proagent_os.professional.kb.embeddings import EmbeddingsError
from proagent_os.professional.kb.retrieve import pack_embedding

# sqlite-vec 0.1.9 refuses KNN k above this. Newer builds may differ; a query
# that still exceeds the build's cap raises VectorStoreError instead of
# falling back to the blob cosine path.
SQLITE_VEC_K_MAX = 4096

_MISSING_PACKAGE = (
    "PROAGENT_KB_VECTOR_STORE=sqlite_vec requires the sqlite-vec package. "
    "Install it with: pip install 'proagent-os[vec]'. "
    "BM25 retrieval (PROAGENT_KB_RETRIEVAL=bm25) does not load this extension."
)

_LOAD_FAILED = (
    "PROAGENT_KB_VECTOR_STORE=sqlite_vec could not load the sqlite-vec extension "
    "({error}). Install it with: pip install 'proagent-os[vec]' and use a Python "
    "build that can load SQLite extensions. "
    "BM25 retrieval (PROAGENT_KB_RETRIEVAL=bm25) does not load this extension."
)


class VectorStoreError(EmbeddingsError):
    """The selected vector index could not be read or written.

    Subclasses :class:`EmbeddingsError` so the KB HTTP routes return 503,
    the same as a missing embeddings API. This is a storage failure, not an
    embedder failure.
    """


class VectorStoreConfigError(VectorStoreError):
    """``sqlite_vec`` is selected but the package or extension is not usable."""


def knn_k(n_chunks: int, *, mode: str, top_k: int) -> int:
    """How many neighbors one sqlite-vec query should return."""
    if n_chunks < 1:
        return 0
    if mode == "vector":
        wanted = min(n_chunks, max(0, int(top_k)))
    else:
        wanted = n_chunks
    return min(wanted, SQLITE_VEC_K_MAX)


def vec_table_name(dims: int) -> str:
    """Virtual table for one embedding width. The width is part of the DDL."""
    width = int(dims)
    if width < 1 or width > 4096:
        raise VectorStoreError(
            f"sqlite-vec index width {width} is outside the supported range 1..4096"
        )
    return f"kb_vec_d{width}"


def _import_sqlite_vec():
    import sqlite_vec

    return sqlite_vec


def load_sqlite_vec_extension(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into ``conn`` or raise :class:`VectorStoreConfigError`."""
    try:
        sqlite_vec = _import_sqlite_vec()
    except ImportError as exc:
        raise VectorStoreConfigError(_MISSING_PACKAGE) from exc
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("SELECT vec_version()").fetchone()
    except VectorStoreConfigError:
        raise
    except Exception as exc:
        raise VectorStoreConfigError(_LOAD_FAILED.format(error=exc)) from exc
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


class BlobVectorStore:
    """Default store. SQL for the BLOB table lives in :meth:`upsert`."""

    kind = "blob"

    def ensure_available(self, db: Database) -> None:
        """Nothing to load. The default install has no native extension."""
        return None

    def upsert(
        self,
        db: Database,
        chunk_id: str,
        tenant_id: str,
        vec: Sequence[float],
        *,
        dims: int,
        model: str,
    ) -> None:
        blob = pack_embedding(vec)
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO kb_chunk_embeddings (chunk_id, tenant_id, dims, embedding, model)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    dims=excluded.dims,
                    embedding=excluded.embedding,
                    model=excluded.model
                """,
                (chunk_id, tenant_id, dims, blob, model),
            )


class SqliteVecVectorStore:
    """sqlite-vec ``vec0`` index, partitioned by tenant, cosine distance.

    One virtual table per embedding width (``kb_vec_d{dims}``) because ``vec0``
    fixes the dimension in DDL. ``kb_vec_index`` records those widths. Neither
    table is created until this store writes a vector, so a blob-only database
    keeps today's schema.
    """

    kind = "sqlite_vec"

    def __init__(self, loader=None) -> None:
        self._loader = loader or load_sqlite_vec_extension

    def ensure_available(self, db: Database) -> None:
        with db.connect(initializer=self._loader):
            return None

    def upsert(
        self,
        db: Database,
        chunk_id: str,
        tenant_id: str,
        vec: Sequence[float],
        *,
        dims: int,
        model: str,
    ) -> None:
        if len(vec) != int(dims):
            raise EmbeddingsError(
                f"embedding width {len(vec)} does not match PROAGENT_KB_EMBED_DIMS={dims}"
            )
        blob = pack_embedding(vec)
        table = vec_table_name(dims)
        with db.connect(initializer=self._loader) as conn:
            self._ensure_schema(conn, int(dims))
            self._delete_chunk(conn, chunk_id)
            conn.execute(
                f"INSERT INTO {table} (chunk_id, tenant_id, model, embedding) VALUES (?, ?, ?, ?)",
                (chunk_id, tenant_id, model, blob),
            )

    def covers(
        self,
        db: Database,
        tenant_id: str,
        chunk_ids: Sequence[str],
        *,
        dims: int,
        model: str,
    ) -> bool:
        """True when every chunk has a vector of this width and model label.

        A missing table is incomplete coverage (BM25 fallback), not a missing
        extension. Call :meth:`ensure_available` first so a missing extension
        raises instead of looking like an empty index.
        """
        if not chunk_ids:
            return True
        table = vec_table_name(dims)
        with db.connect(initializer=self._loader) as conn:
            if not _table_exists(conn, table):
                return False
            rows = conn.execute(
                f"SELECT chunk_id, model FROM {table} WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        found = {row["chunk_id"]: row["model"] for row in rows}
        return all(found.get(chunk_id) == model for chunk_id in chunk_ids)

    def similarities(
        self,
        db: Database,
        tenant_id: str,
        chunk_ids: Sequence[str],
        query_vec: Sequence[float],
        *,
        dims: int,
        model: str,
        mode: str,
        top_k: int,
    ) -> List[float]:
        """Cosine similarity per chunk id, in ``chunk_ids`` order.

        sqlite-vec returns cosine *distance* (``1 - cosine``). Chunks the query
        does not return score 0. That is exact when ``k`` covers the tenant and
        an approximation when hybrid mode is capped at 4096 neighbors.
        """
        if len(query_vec) != int(dims):
            raise EmbeddingsError(
                f"query embedding width {len(query_vec)} does not match "
                f"PROAGENT_KB_EMBED_DIMS={dims}"
            )
        if not chunk_ids:
            return []
        k = knn_k(len(chunk_ids), mode=mode, top_k=top_k)
        if k < 1:
            return [0.0] * len(chunk_ids)
        table = vec_table_name(dims)
        blob = pack_embedding(query_vec)
        try:
            with db.connect(initializer=self._loader) as conn:
                rows = conn.execute(
                    f"""
                    SELECT chunk_id, distance
                    FROM {table}
                    WHERE embedding MATCH ?
                      AND k = ?
                      AND tenant_id = ?
                      AND model = ?
                    """,
                    (blob, k, tenant_id, model),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            raise VectorStoreError(
                "sqlite-vec query failed "
                f"({exc}). PROAGENT_KB_VECTOR_STORE=sqlite_vec is not using "
                "the blob cosine path."
            ) from exc
        by_id = {row["chunk_id"]: 1.0 - float(row["distance"]) for row in rows}
        return [by_id.get(chunk_id, 0.0) for chunk_id in chunk_ids]

    def _ensure_schema(self, conn: sqlite3.Connection, dims: int) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_vec_index (
                dims INTEGER PRIMARY KEY,
                distance_metric TEXT NOT NULL
            )
            """
        )
        table = vec_table_name(dims)
        if not _table_exists(conn, table):
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE {table} USING vec0(
                    chunk_id TEXT PRIMARY KEY,
                    tenant_id TEXT PARTITION KEY,
                    model TEXT,
                    embedding float[{dims}] distance_metric=cosine
                )
                """
            )
        conn.execute(
            """
            INSERT INTO kb_vec_index (dims, distance_metric) VALUES (?, 'cosine')
            ON CONFLICT(dims) DO NOTHING
            """,
            (dims,),
        )

    def _delete_chunk(self, conn: sqlite3.Connection, chunk_id: str) -> None:
        rows = conn.execute("SELECT dims FROM kb_vec_index").fetchall()
        for row in rows:
            table = vec_table_name(int(row["dims"]))
            if _table_exists(conn, table):
                conn.execute(f"DELETE FROM {table} WHERE chunk_id = ?", (chunk_id,))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def get_vector_store(settings: Optional[Settings] = None):
    """Build the store selected by ``PROAGENT_KB_VECTOR_STORE``.

    Constructing the sqlite-vec store does not import the extension. Loading
    happens on :meth:`SqliteVecVectorStore.ensure_available` and on write/search.
    """
    settings = settings or get_settings()
    if settings.kb_vector_store == "blob":
        return BlobVectorStore()
    if settings.kb_vector_store == "sqlite_vec":
        return SqliteVecVectorStore()
    raise VectorStoreConfigError(
        f"Unknown PROAGENT_KB_VECTOR_STORE {settings.kb_vector_store!r}. "
        "Use blob or sqlite_vec."
    )
