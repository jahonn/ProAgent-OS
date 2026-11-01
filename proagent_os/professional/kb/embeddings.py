"""Knowledge-base embedding backends.

``stub`` is an offline hash-bag. ``openai`` POSTs to an OpenAI-compatible
``/v1/embeddings`` endpoint. BM25 search does not call either backend.
"""

from __future__ import annotations

import math
from typing import List, Protocol, Sequence

import httpx

from proagent_os.config import Settings, get_settings
from proagent_os.professional.kb.retrieve import embedding_stub

# OpenAI accepts up to 2048 inputs per request. Stay well under that, and under
# typical per-request token caps, when reindexing a large corpus.
EMBED_BATCH_SIZE = 64
EMBED_TIMEOUT_S = 90.0


class EmbeddingsError(RuntimeError):
    """The embeddings request failed or the payload could not be stored."""


class EmbeddingsConfigError(EmbeddingsError):
    """``openai`` is selected but the base URL, API key, or model id is missing."""


class Embedder(Protocol):
    model: str
    dims: int

    def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        """Return one vector per input, in input order."""


class StubEmbedder:
    """Offline hash-bag. ``model`` stays ``stub`` so existing rows still match."""

    def __init__(self, dims: int) -> None:
        if dims < 1:
            raise ValueError("dims must be positive")
        self.dims = dims
        self.model = "stub"

    def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        return [embedding_stub(text, dims=self.dims) for text in texts]


class OpenAIEmbedder:
    """Sync client for ``POST {base_url}/embeddings``.

    ``base_url`` is the API root ending in ``/v1``, same as the chat client.
    The API key is omitted from ``repr`` so logs do not record it.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dims: int,
        *,
        timeout_s: float = EMBED_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dims = dims
        self.timeout_s = timeout_s

    def __repr__(self) -> str:
        return (
            f"OpenAIEmbedder(base_url={self.base_url!r}, model={self.model!r}, "
            f"dims={self.dims})"
        )

    def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        vectors: List[List[float]] = []
        with httpx.Client(timeout=self.timeout_s) as client:
            for start in range(0, len(texts), EMBED_BATCH_SIZE):
                batch = list(texts[start : start + EMBED_BATCH_SIZE])
                vectors.extend(self._embed_batch(client, batch, headers))
        return vectors

    def _embed_batch(
        self,
        client: httpx.Client,
        texts: List[str],
        headers: dict,
    ) -> List[List[float]]:
        payload = {
            "model": self.model,
            "input": texts,
            "dimensions": self.dims,
        }
        try:
            response = client.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            snippet = exc.response.text[:300].replace("\n", " ").strip()
            raise EmbeddingsError(
                f"embeddings request failed with HTTP {exc.response.status_code}: {snippet}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingsError(
                f"embeddings request failed ({exc.__class__.__name__})"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise EmbeddingsError("embeddings response was not JSON") from exc
        return _parse_embeddings(data, expected=len(texts), dims=self.dims)


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Build the embedder selected by ``PROAGENT_KB_EMBED_BACKEND``."""
    settings = settings or get_settings()
    if settings.kb_embed_backend == "stub":
        return StubEmbedder(dims=int(settings.kb_embed_dims))
    if settings.kb_embed_backend == "openai":
        base_url, api_key, model, dims = openai_embed_target(settings)
        return OpenAIEmbedder(base_url, api_key, model, dims)
    raise EmbeddingsConfigError(
        f"Unknown PROAGENT_KB_EMBED_BACKEND {settings.kb_embed_backend!r}. "
        "Use stub or openai."
    )


def openai_embed_target(settings: Settings | None = None) -> tuple[str, str, str, int]:
    """Resolve ``(base_url, api_key, model, dims)`` for the openai backend.

    An empty embeddings base URL or API key reuses the chat client settings.
    A missing value raises :class:`EmbeddingsConfigError` and does not switch
    the caller over to the stub.
    """
    settings = settings or get_settings()
    base_url = (settings.kb_embed_base_url or settings.llm_base_url or "").strip()
    api_key = (settings.kb_embed_api_key or settings.llm_api_key or "").strip()
    model = (settings.kb_embed_model or "").strip()
    dims = int(settings.kb_embed_dims)
    if not base_url:
        raise EmbeddingsConfigError(
            "PROAGENT_KB_EMBED_BACKEND=openai needs a base URL. "
            "Set PROAGENT_KB_EMBED_BASE_URL or PROAGENT_LLM_BASE_URL "
            "to an OpenAI-compatible /v1 root."
        )
    if not api_key:
        raise EmbeddingsConfigError(
            "PROAGENT_KB_EMBED_BACKEND=openai needs an API key. "
            "Set PROAGENT_KB_EMBED_API_KEY or PROAGENT_LLM_API_KEY. "
            "BM25 search does not call the embeddings endpoint."
        )
    if not model:
        raise EmbeddingsConfigError(
            "PROAGENT_KB_EMBED_MODEL is empty. "
            "Set it to the embeddings model id (for example text-embedding-3-small)."
        )
    if dims < 1:
        raise EmbeddingsConfigError("PROAGENT_KB_EMBED_DIMS must be positive")
    return base_url, api_key, model, dims


def _parse_embeddings(data: object, *, expected: int, dims: int) -> List[List[float]]:
    if not isinstance(data, dict):
        raise EmbeddingsError("embeddings response was not a JSON object")
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) != expected:
        raise EmbeddingsError(
            f"embeddings response data length does not match the {expected} inputs"
        )
    ordered: List[List[float] | None] = [None] * expected
    for row in rows:
        if not isinstance(row, dict):
            raise EmbeddingsError("embeddings response data item was not an object")
        index = _as_index(row.get("index", 0 if expected == 1 else None), expected)
        if ordered[index] is not None:
            raise EmbeddingsError(f"duplicate embedding index {index}")
        embedding = row.get("embedding")
        if not isinstance(embedding, list):
            raise EmbeddingsError(
                "embedding must be a JSON array of numbers (the float encoding)"
            )
        if len(embedding) != dims:
            raise EmbeddingsError(
                f"embedding width {len(embedding)} does not match PROAGENT_KB_EMBED_DIMS={dims}"
            )
        ordered[index] = [_as_float(value) for value in embedding]
    if any(vector is None for vector in ordered):
        raise EmbeddingsError("embeddings response missed an index")
    return [vector for vector in ordered if vector is not None]


def _as_index(value: object, expected: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EmbeddingsError("embedding index must be an integer")
    if not 0 <= value < expected:
        raise EmbeddingsError(f"embedding index {value} is outside 0..{expected - 1}")
    return value


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EmbeddingsError("embedding values must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise EmbeddingsError("embedding values must be finite numbers")
    return number
