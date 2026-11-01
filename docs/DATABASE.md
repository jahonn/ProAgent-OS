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
