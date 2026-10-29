# Mock demo

Offline path. No API key and no network call to a model. The script forces `PROAGENT_LLM_PROVIDER=mock` when that variable is unset, writes a gitignored SQLite file, and prints each stage.

Identifiers (`ten_…`, `task_…`, `mon_…`, `rec_…`, `out_…`, `mem_…`) change every run. The titles, plan, mock answer, skill list, and agent list below are stable on the current fixtures.

## Watch it first

The picture is a real run of the mock path. Nothing to install if you only want to watch.

[![ProAgent OS mock demo](demo.svg)](demo.cast)

- [`demo.svg`](demo.svg) — animated terminal (SMIL). It plays in a browser and in the README on GitHub.
- [`demo.cast`](demo.cast) — the same session as asciinema v2.

```bash
ascinema play docs/demo.cast
```

What was recorded, with `PROAGENT_LLM_PROVIDER=mock`, no API key, and no model network call:

```bash
python scripts/demo.py
python scripts/demo_support_desk.py
```

`scripts/demo.py` is the full path: tenant, knowledge, skills, proactive recommendations, and metrics loop. It writes a demo file `tmp/proagent.db` at each stage so the DATA sees everything.

`scripts/demo_support_desk.py` uses the desk app as the analyst tool (call). The dashboard tab shows the same output metric as the script.

Optional worker, same environment so it shares the file:

```bash
python -m proagent_os.worker
```

## Fresh API database

Skip the demo file when you want an empty app:

```bash
export PROAGENT_ADMIN_KEY=dev-admin-key
export PROAGENT_LLM_PROVIDER=mock
export PROAGENT_DB_PATH=./data/proagent.db
uvicorn proagent_os.main:app --host 0.0.0.0 --port 8080
```

```bash
curl -s -X POST http://localhost:8080/api/v1/tenants \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme","goals":"Grow conversion","tone":"concise"}'
```

The JSON includes a new `api_key`. Use that bearer token for tenant routes. `dev-admin-key` can list tenants; it is the bootstrap admin key from the environment, not a tenant key.

Docker Compose (`docker compose up --build`) also starts from an empty volume. Create a tenant the same way. It does not mount `data/demo.db`.

## Troubleshooting

### `ModuleNotFoundError: No module named 'proagent_os'`
Install the package in development mode:
```bash
pip install -e .
```

### `PROAGENT_LLM_PROVIDER=mock` but still getting API errors
Ensure the environment variable is set **before** importing the package. In shell:
```bash
export PROAGENT_LLM_PROVIDER=mock
python scripts/demo.py
```

### SQLite database locked / `OperationalError: database is locked`
- Only one process can write to the SQLite file at a time.
- Stop any running worker or API server before running the demo.
- Delete `tmp/proagent.db` (or `data/proagent.db`) to start fresh.

### `ascinema play docs/demo.cast` shows nothing
- Ensure you have asciinema v2 installed: `pip install asciinema` or `brew install asciinema`.
- The cast file uses SMIL animation; open `demo.svg` in a browser for the animated version.

### Port 8080 already in use
Change the port:
```bash
uvicorn proagent_os.main:app --port 8081
```

### `401 Unauthorized` on tenant routes
- Use the tenant's `api_key` (returned from `POST /api/v1/tenants`), not the admin key.
- Admin key (`dev-admin-key`) only works for `POST /api/v1/tenants` and admin endpoints.

### Skills not discovered
- Skills are loaded from `skills/*/SKILL.md` at startup.
- Restart the API server after adding a new skill directory.
- Verify `SKILL.md` has valid front-matter (`id`, `name`, `description`).

### Proactive recommendations not appearing
- The proactive loop runs on a background scheduler.
- Check `PROAGENT_EXECUTOR_MODE` — set to `record` to log decisions without executing.
- Set `PROAGENT_EXECUTOR_WEBHOOK_URL` if you want webhook delivery.

### Rate limiting / `429 Too Many Requests`
- The rate limit is a per-process stub. It does not survive a restart.
- Approve and execute are privileged. A tenant API key that can approve can trigger the configured executor.
- Webhook mode `POST`s only to `PROAGENT_EXECUTOR_WEBHOOK_URL`. The recommendation `id` is an idempotency key. A crash after a successful `POST` can retry the same recommendation.
- Set `PROAGENT_EXECUTOR_MODE=off` when approve should record a decision and nothing else. Leave `PROAGENT_EXECUTOR_WEBHOOK_TOKEN` empty unless the receiver requires it. The token is not copied into the audit row or the API response.
- Webhook receiver should treat `recommendation_id` as an idempotency key. A crash after a successful `POST` can retry the same recommendation.

## License

MIT — see [LICENSE](LICENSE).