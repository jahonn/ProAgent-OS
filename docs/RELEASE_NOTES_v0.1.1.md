# ProAgent OS v0.1.1

Human release notes for the published **v0.1.1** cut — the first public GitHub Release (2026-11-02).

**Cut:** annotated tag `v0.1.1` on `main` after Harness P2.5 (#27–#32), including plan-and-replan (#32).
**Canonical detail:** [CHANGELOG.md](../CHANGELOG.md) `[0.1.1]`.

Package version is `0.1.1` (`pyproject.toml` and `proagent_os.__version__`). The historical changelog section `[0.1.0] - 2026-10-29` stays the earlier MVP write-up.

## Highlights

### Harness
- Explicit agent-loop phases (`LoopPhase`) and industry mapping docs ([ARCHITECTURE.md](ARCHITECTURE.md)).
- Golden-task / tool-trajectory eval stub with ordered-subset matching ([EVAL.md](EVAL.md)).
- Task-run HITL pause/resume stub (`awaiting_human`, `request_run_approval`).
- Session/context compaction stub ([MEMORY.md](MEMORY.md)).
- Tool JSON-schema validation before execute ([TOOLS.md](TOOLS.md)).
- Optional plan-and-replan outer loop stub (`PROAGENT_REPLAN_*`, default off).

### Auth / Ops
- Roles sketch + env-gated OIDC mode (still API-key default; no IdP call) ([AUTH.md](AUTH.md)).
- Optional OpenTelemetry hooks (`otel` extra) and usage metering stub (`billing: stub`).
- Versioned SQLite schema migrations + backup/restore notes ([DATABASE.md](DATABASE.md)).
- Tenant API key rotate/revoke, in-process rate-limit stub, recommendation executors (`off` / `noop` / `webhook`).

### KB
- OpenAI-compatible embeddings backend (default remains stub + BM25).
- Optional `sqlite-vec` vector index behind the `vec` extra (default remains blob + Python cosine).

### Product surface
- Support desk vertical pack + recorded mock demo (`docs/demo.svg` / `docs/demo.cast`).
- README positioning / scan path for first-time visitors; `/health` exposes package version.

### Docs
- Architecture, auth, eval, memory, tools, database, vertical skill guide, and demo troubleshooting.

## What this release is
- The human notes for the published **v0.1.1** GitHub Release.
- Detail lives in CHANGELOG `[0.1.1]`. The `[0.1.0]` section is historical and unchanged.

## See also
- [ARCHITECTURE.md](ARCHITECTURE.md) · [AUTH.md](AUTH.md) · [EVAL.md](EVAL.md) · [MEMORY.md](MEMORY.md) · [TOOLS.md](TOOLS.md) · [DATABASE.md](DATABASE.md)
- Roadmap: [issue #2](https://github.com/jahonn/ProAgent-OS/issues/2)
