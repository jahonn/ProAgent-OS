# Database

ProAgent OS keeps each deployment's tenant data in one SQLite file. `PROAGENT_DB_PATH` sets the path (default `./data/proagent.db`). The API and the worker share that file. SQLite allows one writer at a time, so a second process waits on the write lock instead of running its own copy of the data.

Do not commit the database, a backup of it, or the `-wal` and `-shm` sidecars. `.gitignore` already ignores `data/*.db`.

## Migrations

Opening the database applies pending migrations. That happens when the API starts, when the worker starts, and when you run:

```bash
python -m proagent_os.migrations
```

The command prints the file path and the rows in `schema_migrations` (`version`, `name`, `applied_at`). It reads `PROAGENT_DB_PATH` from the environment. A second run against a current file prints `applied now: none (already current)`.

Versions are the ordered steps in `proagent_os/migrations.py`. Each step is SQL, Python, or both. A version already recorded is skipped. The runner takes a write lock for the duration of the pending steps, so the API and the worker can start together and only one of them applies a given version.

`0001_baseline` is the schema this repository used to create with `CREATE TABLE IF NOT EXISTS`, including `tenants.key_status`. An empty file runs that script, then every later version. A file that already has those tables and indexes, and has no `schema_migrations` rows, is stamped: version 1 is recorded and the baseline script is not executed. Rows already in the file stay where they are. Versions after 1 still run.

`0002_tenant_list_indexes` is the first forward step. It adds `idx_tasks_tenant_created`, `idx_kb_chunks_tenant`, and `idx_recommendations_tenant_status`. It does not rewrite existing rows.

`0003_usage_events` adds the usage-metering stub table `usage_events` (`tenant_id`, `kind`, `quantity`, `created_at`) and `idx_usage_events_tenant_kind`. Kinds are `llm_tokens`, `kb_search`, and `recommendation_execute`. The table is a local counter. It is not a payment ledger, and this migration does not import OpenTelemetry.

`0005_task_hitl_pause` adds task-run HITL columns on `tasks`: `pause_reason`, `pending_action_json` (opaque JSON, never treated as a URL or shell command), `hitl_decision`, `hitl_decided_at`, plus `idx_tasks_tenant_status`. A gated-tool pause stores a snapshot in that column (`kind` `gated_tool`, tool name, validated `arguments`, opaque `proposed`). Approve may re-invoke only that snapshot through the tool registry. Status values include `running`, `awaiting_human`, `completed`, and `cancelled`. `0006_hitl_checkpoint` adds `hitl_checkpoint_json` (orchestrator resume state: plan, completed steps, pause locus, and the argument snapshot — also opaque, never URL/shell).

`0007_hitl_execute_receipts` adds `hitl_execute_receipts`, the crash-safe exactly-once guard for execute-on-approve. Columns: `id`, `tenant_id`, `task_id`, `tool_call_id`, `tool_name`, `args_hash`, `fingerprint` (unique), `status` (`started`, `succeeded`, or `failed`), `observation_json` (snippet, opaque JSON, not a URL or shell command), `error`, `created_at`, `updated_at`. `fingerprint` is the SHA-256 of canonical `{task_id, tool, args_hash}`; the raw arguments are not stored as their own column. Indexes: `idx_hitl_execute_receipts_fingerprint` (unique) and `idx_hitl_execute_receipts_task` on `(tenant_id, task_id, created_at)`. A row is inserted as `started` before the gated tool runs and updated with the observation before the task leaves `awaiting_human`. A `succeeded` or `failed` row is reused on a later resume and is not invoked again. That includes a task left in `running` after the receipt was stored and before `completed`: `POST /api/v1/tasks/{id}/resume` re-injects `observation_json` and finishes the open `hitl_checkpoint_json`. It does not insert another row and does not run the tool. A `started` row is not run again unless the task is still `awaiting_human` and the resume body sets `reexecute` true. A `running` task with a `started` row fails closed (HTTP 409), including when `reexecute` is true. `POST .../reject` cancels `awaiting_human`, and also `running` when `hitl_checkpoint_json` is still an open checkpoint, and does not execute. `GET /api/v1/tasks/{id}` and each `GET /api/v1/tasks` item include `hitl_execute_receipts`, a summary of these rows (tool name, fingerprint prefix, args hash, status, timestamps, capped observation and error). That JSON omits the raw argument object and the full fingerprint, and it replaces secret-like values with `[redacted]`. The read uses the same tenant scope as the task itself. See [ARCHITECTURE.md](ARCHITECTURE.md).

`0004_tenant_role_overrides` adds `tenant_role_overrides` (`tenant_id`, `role`, `updated_at`). `role` is `tenant_operator` or `tenant_readonly`. It records an API-key role for one tenant. An empty table means every tenant key is `tenant_operator`. The admin key is not a row in this table. See [AUTH.md](AUTH.md).

A partial file (for example a `tenants` table from before `key_status` existed, without the rest of the baseline tables) is not stamped. Version 1 creates the missing tables and adds `key_status` when that column is absent.

sqlite-vec virtual tables (`kb_vec_index`, `kb_vec_d*`) are not part of these migrations. The vector store creates them when `PROAGENT_KB_VECTOR_STORE=sqlite_vec` and the `vec` extra is installed. Migrating does not import `sqlite_vec`.

To add a schema change, append the next integer version in `proagent_os/migrations.py`. Leave a released version as it was written. Databases that have recorded it will not run an edited script. Prefer additive SQL (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`). A backup whose `schema_migrations` version is newer than this build, or whose version numbers skip, is refused at startup so the process does not guess.

## Backup

Stop the API and the worker, then copy the file:

```bash
cp data/proagent.db data/proagent-backup.db
```

The default journal mode is DELETE, so a stopped process usually has only that one file. If `data/proagent.db-wal` or `data/proagent.db-shm` is present, copy those next to the backup as well, or checkpoint first while the process is still the one holding the file (`PRAGMA wal_checkpoint(TRUNCATE)`) and then copy the main file alone. A copy taken while a writer is mid-transaction can land on a torn page. Stop the processes, or use the online backup below.

Docker Compose stores the file in the `proagent-data` volume at `/app/data/proagent.db`. Stop the stack before copying it out of the volume.

## Online backup

SQLite's backup API copies a consistent snapshot while the process is running, including when WAL mode is on. The snapshot includes committed pages only. The Python standard library is enough; the Docker image does not install the `sqlite3` shell.

```bash
python - <<'PY'
import sqlite3
src = sqlite3.connect("data/proagent.db")
dst = sqlite3.connect("data/proagent-backup.db")
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
```

When the `sqlite3` shell is installed, the same API is `sqlite3 data/proagent.db ".backup data/proagent-backup.db"`. Inside Compose, run the Python snippet with paths under `/app/data`. The result is a standalone database file. Keep it outside the git worktree.

## Restore

Stop the API and the worker. Replace the live file with the backup. Delete a leftover `proagent.db-wal` or `proagent.db-shm` beside the live path so SQLite does not replay an old journal over the restored pages. Start the API or the worker again, or run `python -m proagent_os.migrations` first.

Startup applies any migration versions newer than the ones recorded in the backup. Restoring an older file moves the schema forward to this build. This project does not downgrade a file. A backup written by a newer build, with a schema version this process does not know, is refused until you upgrade.
