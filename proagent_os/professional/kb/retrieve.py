from __future__ import annotations

import hashlib
import math
import re
import struct
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple


_TOKEN = re.compile(r"[a-z0-9_]+", re.I)


def tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def bm25_scores(
    query: str,
    documents: Sequence[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> List[float]:
    """Simple offline BM25-style ranking (no external deps)."""
    q_tokens = tokenize(query)
    if not q_tokens or not documents:
        return [0.0] * len(documents)

    docs_tokens = [tokenize(d) for d in documents]
    N = len(docs_tokens)
    avgdl = sum(len(d) for d in docs_tokens) / max(N, 1)

    df: Dict[str, int] = Counter()
    for toks in docs_tokens:
        for term in set(toks):
            df[term] += 1

    scores: List[float] = []
    for toks in docs_tokens:
        tf = Counter(toks)
        dl = len(toks) or 1
        score = 0.0
        for term in q_tokens:
            if term not in tf:
                continue
            n_qi = df.get(term, 0)
            idf = math.log(1 + (N - n_qi + 0.5) / (n_qi + 0.5))
            freq = tf[term]
            denom = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf * (freq * (k1 + 1)) / denom
        scores.append(score)
    return scores


def _stable_token_hash(token: str) -> int:
    """Process-stable token hash. builtin hash() is salted per interpreter."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little", signed=False)


def embedding_stub(text: str, dims: Optional[int] = None) -> List[float]:
    """Deterministic hash-bag embedding for the offline stub backend.

    Not a semantic model. Same tokens always land in the same dimensions
    across processes. ``dims`` defaults to ``PROAGENT_KB_EMBED_DIMS`` (64).
    """
    if dims is None:
        from proagent_os.config import get_settings

        dims = int(get_settings().kb_embed_dims)
    if dims < 1:
        raise ValueError("dims must be positive")
    vec = [0.0] * dims
    for tok in tokenize(text):
        h = _stable_token_hash(tok)
        vec[h % dims] += 1.0
        vec[(h // dims) % dims] += 0.5
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def pack_embedding(vec: Sequence[float]) -> bytes:
    """Little-endian float32 blob."""
    return struct.pack(f"<{len(vec)}f", *[float(v) for v in vec])


def unpack_embedding(blob: bytes) -> List[float]:
    raw = bytes(blob)
    if len(raw) % 4 != 0:
        raise ValueError("embedding blob length is not a multiple of 4")
    count = len(raw) // 4
    return list(struct.unpack(f"<{count}f", raw))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def normalize_scores(scores: Sequence[float]) -> List[float]:
    """Scale a signal by its maximum so the best candidate is 1.

    Non-positive values contribute 0. An all-zero (or negative) signal
    stays all zeros. Order of strictly positive scores is unchanged.
    """
    if not scores:
        return []
    peak = max(scores)
    if peak <= 0.0:
        return [0.0] * len(scores)
    return [s / peak if s > 0.0 else 0.0 for s in scores]


def hybrid_scores(
    bm25: Sequence[float],
    vector: Sequence[float],
    alpha: float = 0.5,
) -> List[float]:
    """``alpha * norm(vector) + (1 - alpha) * norm(bm25)`` on one candidate set."""
    if len(bm25) != len(vector):
        raise ValueError("bm25 and vector scores must be the same length")
    weight = min(1.0, max(0.0, float(alpha)))
    lexical = normalize_scores(bm25)
    semantic = normalize_scores(vector)
    return [
        weight * vec + (1.0 - weight) * lex
        for lex, vec in zip(lexical, semantic)
    ]


def rank_chunks(query: str, chunks: Sequence[Dict], top_k: int = 5) -> List[Tuple[Dict, float]]:
    docs = [c.get("content", "") for c in chunks]
    scores = bm25_scores(query, docs)
    ranked = sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)
    return [(c, s) for c, s in ranked[:top_k] if s > 0]


def rank_chunks_with_embeddings(
    query: str,
    chunks: Sequence[Dict],
    embeddings: Sequence[Sequence[float]],
    *,
    mode: str = "hybrid",
    alpha: float = 0.5,
    top_k: int = 5,
    dims: int = 64,
    query_embedding: Optional[Sequence[float]] = None,
) -> List[Tuple[Dict, float]]:
    """Rank chunks by cosine (``vector``) or hybrid fusion.

    ``mode='bm25'`` or a length mismatch delegates to :func:`rank_chunks`.
    When ``query_embedding`` is omitted, the offline stub embedder is used
    so callers that do not supply a vector stay deterministic.
    """
    if mode == "bm25" or len(chunks) != len(embeddings):
        return rank_chunks(query, chunks, top_k=top_k)
    if query_embedding is None:
        query_vec = embedding_stub(query, dims=dims)
    else:
        query_vec = [float(value) for value in query_embedding]
    vector_scores = [cosine_similarity(query_vec, emb) for emb in embeddings]
    if mode == "vector":
        scores = vector_scores
    else:
        docs = [c.get("content", "") for c in chunks]
        scores = hybrid_scores(bm25_scores(query, docs), vector_scores, alpha=alpha)
    ranked = sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)
    return [(c, s) for c, s in ranked[:top_k] if s > 0]
