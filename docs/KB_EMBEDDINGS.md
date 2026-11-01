# Knowledge base embeddings

The default knowledge base retrieval mode is BM25. It uses lexical search and
does not call an embeddings API or load a native vector extension. The default
embedder setting is `stub`, an offline hash-bag; the default vector store is
`blob`.

## Use an OpenAI-compatible embeddings API

Set `PROAGENT_KB_RETRIEVAL` to `hybrid` to combine vector similarity with
BM25, or to `vector` for cosine similarity only. Then configure the embedder:

```bash
export PROAGENT_KB_RETRIEVAL=hybrid
export PROAGENT_KB_EMBED_BACKEND=openai
export PROAGENT_KB_EMBED_MODEL=text-embedding-3-small
export PROAGENT_KB_EMBED_DIMS=1536
export PROAGENT_KB_EMBED_BASE_URL=https://api.openai.com/v1
# Set PROAGENT_KB_EMBED_API_KEY in your environment; do not commit it.
```

The base URL must be an OpenAI-compatible `/v1` API root. If
`PROAGENT_KB_EMBED_BASE_URL` is empty, Pulse uses
`PROAGENT_LLM_BASE_URL`. If `PROAGENT_KB_EMBED_API_KEY` is empty, it uses
`PROAGENT_LLM_API_KEY`. The configured model and dimensions are sent in each
embeddings request. Check the provider's supported dimensions for the selected
model.

When this backend is selected and retrieval is `hybrid` or `vector`, chunk
text and search queries are sent to the configured embeddings endpoint. A
missing base URL, API key, or model, or a failed API request, returns HTTP 503
from the knowledge-base ingest and search endpoints. Pulse does not silently
switch to the stub. BM25 does not call the embedder, even when the backend is
set to `openai`.

## Optional sqlite-vec index

The default `blob` store writes float32 vectors to SQLite and calculates cosine
similarity in Python. To use the optional sqlite-vec `vec0` index instead,
install the extra and select the store:

```bash
pip install 'proagent-os[vec]'
export PROAGENT_KB_VECTOR_STORE=sqlite_vec
```

This setting affects hybrid and vector retrieval. BM25 never imports or loads
sqlite-vec, so it remains available without the extra. The Python build must
also allow SQLite extension loading. If sqlite-vec is unavailable while the
selected retrieval mode needs it, ingest and search return HTTP 503; they do not
fall back to the blob store.

## Reindex after configuration changes

Hybrid and vector retrieval need stored embeddings for the active model,
dimensions, and vector store. Existing chunks with missing vectors or vectors
from a different model or width fall back to BM25. After changing the embedder,
model, dimensions, or vector store, reindex the affected tenant using the current
environment configuration:

```python
from proagent_os.professional.kb.store import KnowledgeBase

KnowledgeBase().reindex_embeddings(tenant_id="YOUR_TENANT_ID")
```

The method is safe to run again. With the `openai` backend, reindexing sends
the tenant's stored chunks to the configured embeddings endpoint. Use
`PROAGENT_KB_RETRIEVAL=bm25` if you want lexical search without embeddings.
