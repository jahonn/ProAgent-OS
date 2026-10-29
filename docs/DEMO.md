# Mock demo

Offline path. No API key and no network call to a model. The script forces `PROAGENT_LLM_PROVIDER=mock` when that variable is unset, writes a gitignored SQLite file, and prints each stage.

Identifiers (`ten_…`, `task_…`, `mon_…`, `rec_…`, `out_…`, `mem_…`) change every run. The titles, plan, mock answer, skill list, and agent list below are stable on the current fixtures.

## Watch it first

The picture is a real run of the mock path. Nothing to install if you only want to watch.

[![ProAgent OS mock demo](demo.svg)](demo.cast)

- [`demo.svg`](demo.svg) — animated terminal (SMIL). It plays in a browser and in the README on GitHub.
- [`demo.cast`](demo.cast) — the same session as asciinema v2.

```bash
asciinema play docs/demo.cast
```

What was recorded, with `PROAGENT_LLM_PROVIDER=mock`, no API key, and no model network call:

```bash
python scripts/demo.py
python scripts/demo_support_desk.py
```

`scripts/demo.py` is the full path: tenant, knowledge base, task, monitor, noop executor, outcome. The support-desk script is the shorter second beat (policy search, `ticket_triage`, and a follow-up the noop executor marks executed without sending a reply).

Lines longer than 100 columns are wrapped on spaces so the frame stays readable. The words are the scripts' stdout. Identifiers change when the recording is regenerated.

Re-record from the repo root after the install in [Setup](#setup):

```bash
python scripts/record_demo.py
```

That overwrites `docs/demo.cast` and `docs/demo.svg`. Child processes force the mock provider, drop provider credentials, and use a temporary SQLite file. No database is committed.

## Setup

From a fresh clone:

```bash
git clone https://github.com/jahonn/ProAgent-OS.git
cd ProAgent-OS
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

Python 3.11 or newer. The demo needs the install above; it does not need Docker or a `.env` file.

## Run the script

```bash
python scripts/demo.py
```

That command is the first half of the recording in [Watch it first](#watch-it-first). `python scripts/record_demo.py` runs it again, then `python scripts/demo_support_desk.py`, and refreshes `docs/demo.cast` and `docs/demo.svg`.

What the script sets for you (only if the variable is unset):

| Variable | Value |
|----------|--------|
| `PROAGENT_DB_PATH` | `./data/demo.db` |
| `PROAGENT_ADMIN_KEY` | `dev-admin-key` |
| `PROAGENT_LLM_PROVIDER` | `mock` |

The script then prints the dashboard JSON and:

```text
Demo complete. Start API with: uvicorn proagent_os.main:app --reload --port 8080
Use Authorization: Bearer pk_demo_acme_analytics
```

## Open the console on the demo database

`data/demo.db` holds the tenant. Point the API at that file or the key will not match.

```bash
export PROAGENT_ADMIN_KEY=dev-admin-key
export PROAGENT_LLM_PROVIDER=mock
export PROAGENT_DB_PATH=./data/demo.db
uvicorn proagent_os.main:app --host 0.0.0.0 --port 8080
```

- http://localhost:8080 — header **ProAgent OS**, badge `v0.1.0`, sidebar tabs Task console, Knowledge base, Recommendations, Skills, Monitors, Dashboard. Paste `pk_demo_acme_analytics` and choose **Save key**.
- http://localhost:8080/health — `{"status":"ok","version":"0.1.0","llm":"mock"}` when the provider is mock.
- http://localhost:8080/docs — FastAPI reference for `/api/v1/*`.

Recommendations for this tenant: one `executed` (the monitor, via the noop executor) and one `pending` (the analyst tool call). The dashboard tab shows the same outcome metric as the script.

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
curl -s -X POST http://localhost:8080/api/v1/tenants   -H "Authorization: Bearer dev-admin-key"   -H "Content-Type: application/json"   -d '{"name":"Acme","goals":"Grow conversion","tone":"concise"}'
```

The JSON includes a new `api_key`. Use that bearer token for tenant routes. `dev-admin-key` can list tenants; it is the bootstrap admin key from the environment, not a tenant key.

Docker Compose (`docker compose up --build`) also starts from an empty volume. Create a tenant the same way. It does not mount `data/demo.db`.

## Troubleshooting

Common issues when running the mock demo or starting the API:

- **Forgot to activate the virtual environment or using the wrong Python.**  
  Run `source .venv/bin/activate` (or `.venv\Scripts\activate` on Windows) after creating the venv. Confirm with `python --version` (must be 3.11+). Reinstall dependencies if the environment is not active: `pip install -r requirements.txt && pip install -e .`.

- **Port 8080 already in use when starting uvicorn.**  
  Another process is bound to the port. Stop it or use a different port: `uvicorn proagent_os.main:app --reload --port 8081`. Check with `lsof -i :8080` (macOS/Linux) or `netstat -ano | findstr :8080` (Windows).

- **Using a fresh database vs the demo database after `scripts/demo.py`.**  
  The demo script writes to `./data/demo.db` (via `PROAGENT_DB_PATH`). To reuse it with the API, export `PROAGENT_DB_PATH=./data/demo.db`. For a clean start, omit it or set `PROAGENT_DB_PATH=./data/proagent.db`. See the environment table in the root README.

- **`PROAGENT_LLM_PROVIDER=openai` without `PROAGENT_LLM_API_KEY`.**  
  The provider falls back or fails. For offline use keep `PROAGENT_LLM_PROVIDER=mock` (the default when unset). Real keys belong in a `.env` file that is gitignored; never commit them.

- **Rate limit 429 during heavy local requests.**  
  The API enforces rate limits. Adjust via `PROAGENT_RATE_LIMIT_*` environment variables (see root README). For local testing, temporarily raise the limits or add short delays between curls.
