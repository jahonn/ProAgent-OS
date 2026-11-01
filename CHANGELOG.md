# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- SQLite schema migrations. Startup (and `python -m proagent_os.migrations`) records versions in `schema_migrations` and applies pending steps in order. Version `0001_baseline` is the previous `CREATE TABLE IF NOT EXISTS` schema, including `tenants.key_status`. A database that already has that schema is stamped at version 1 and the baseline script is not run again. Version `0002_tenant_list_indexes` adds indexes on `tasks`, `kb_chunks`, and `recommendations`. sqlite-vec virtual tables stay on demand and are not part of these steps. Backup and restore are documented in `docs/DATABASE.md`.
- OpenAI-compatible knowledge-base embeddings. `PROAGENT_KB_EMBED_BACKEND=openai` POSTs to `{base}/embeddings`, reusing `PROAGENT_LLM_BASE_URL` and `PROAGENT_LLM_API_KEY` unless `PROAGENT_KB_EMBED_BASE_URL` or `PROAGENT_KB_EMBED_API_KEY` is set. `PROAGENT_KB_EMBED_MODEL` (default `text-embedding-3-small`) and `PROAGENT_KB_EMBED_DIMS` are sent as `model` and `dimensions`. The default backend remains `stub` and the default search mode remains BM25, so CI stays offline. A missing key, a missing base URL, or a failed call raises, and the KB HTTP routes return 503. pgvector is not wired.
- Optional sqlite-vec knowledge-base index. `PROAGENT_KB_VECTOR_STORE=sqlite_vec` (install `pip install 'proagent-os[vec]'`, `sqlite-vec>=0.1.9`) stores chunk vectors in a `vec0` table and runs hybrid/vector KNN in the extension. The default remains `blob`: float32 rows in `kb_chunk_embeddings` and cosine in Python. BM25 never loads sqlite-vec. A missing package or an extension that fails to load raises `VectorStoreConfigError` (HTTP 503) instead of silently using the blob path. CI does not install the extra; the extension test skips when it cannot load. pgvector is still not wired.
- Recorded mock demo: `docs/demo.svg` (SMIL terminal animation) and `docs/demo.cast` (asciinema v2), captured from `scripts/demo.py` and a short `scripts/demo_support_desk.py` beat with `PROAGENT_LLM_PROVIDER=mock`. Regenerate with `python scripts/record_demo.py`.
- Tenant API keys can be rotated and revoked. `POST /api/v1/tenants/me/api-key/rotate` and `.../revoke` use the tenant bearer. Admin uses `POST /api/v1/tenants/{tenant_id}/api-key/rotate` and `.../revoke`. Rotate is a hard cutover (no grace window). A revoked key returns 401 until an admin rotate mints a replacement. `tenant_api_key_audit` stores a prefix and SHA-256, not the raw key. Existing databases gain `tenants.key_status` on startup.
- In-process fixed-window rate limit on `/api/v1` (`PROAGENT_RATE_LIMIT_ENABLED`, `PROAGENT_RATE_LIMIT_REQUESTS`, `PROAGENT_RATE_LIMIT_WINDOW_SECONDS`). Over the limit the response is HTTP 429 `{"detail":"Rate limit exceeded"}` with `Retry-After`. No Redis. Counters are per process.
- Recommendation executors: `off`, `noop` (default), and `webhook`. Approve writes `recommendation_audit` (`decided`, `executed`, `execute_failed`). Webhook POSTs only to `PROAGENT_EXECUTOR_WEBHOOK_URL`; the action payload is JSON, not a URL or command. The mock demo marks the monitor recommendation executed without network.
- Support desk vertical (`verticals/support_desk/`): sample policies, `support_triage`, `ticket_triage` and `policy_lookup` skills, and `scripts/demo_support_desk.py`. Startup merges every `verticals/*/agents/agents.yaml`. The support demo approves through the default noop executor.
- README scan path for first-time visitors: one-line product description, who it is for, a positioning table, and a harness architecture diagram.
- Scripted mock-demo notes in `docs/DEMO.md` (commands and the output to expect). No API key required.
- This changelog. The `v0.1.0` GitHub Release tag is not published yet; cutting it is tracked in [issue #2](https://github.com/jahonn/ProAgent-OS/issues/2).

## [0.1.0] - 2026-10-29

First public MVP. `pyproject.toml` and `proagent_os.__version__` are `0.1.0`. Early open source: demos, vertical scaffolding, and self-hosted experiments.

### Added

- Agent harness: planner → specialists → aggregator, YAML agent registry, SQLite task and trace memory.
- Commercial surface: FastAPI `/api/v1`, one Bearer API key per tenant, a small web console, and Docker Compose (`api` + `worker` on a shared SQLite volume).
- Private knowledge base: URL/HTML and file ingest (md/txt/html, PDF best-effort), per-tenant BM25 search, brand context, and skill packs. Built-in tools: `http_fetch`, `file_read`, `kb_search`, `clock`, `emit_recommendation`.
- Proactive monitors (`kb_change`, `metric_stub`, `data_source`) that write a recommendation inbox. Approve and reject update status. They do not execute the proposed action.
- Outcome loop: `record_outcome`, tenant-memory feedback, and a simple skill-weight nudge (not a trained policy).
- Native OpenAI-style tool calling with a multi-round specialist loop, plus a JSON-in-content fallback. Planner and aggregator stay text-only. `PROAGENT_LLM_PROVIDER=mock` runs with no API key.
- GitHub Actions CI: `pytest` on Python 3.11 and 3.12 with the mock LLM, and a pull request template.
- Optional `hybrid` and `vector` retrieval using an offline hash-bag stub embedder (`PROAGENT_KB_EMBED_BACKEND=stub`). Default search remains BM25, which is what CI runs. sqlite-vec, pgvector, and remote embedding providers are not wired; the `vec` extra is a placeholder.

[Unreleased]: https://github.com/jahonn/ProAgent-OS/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jahonn/ProAgent-OS/releases/tag/v0.1.0
