# Ops incident vertical

MVP pack for a private on-call runbook. It is a demo you can run offline. It is not a production incident system, and it does not page anyone.

What you get:

- Agent `incident_commander` (concise, must cite the runbook, files a follow-up when a person should decide).
- Skills `incident_triage` and `runbook_lookup` (`SKILL.md` plus a deterministic `run.py`).
- Three short Harborline runbooks in `fixtures/`.
- `scripts/demo_ops_incident.py` — tenant, ingest, search, skill, mock task, and a monitor recommendation.

`run.py` ranks keywords. It does not call a model. A real `PROAGENT_LLM_PROVIDER=openai` run can still select `incident_commander`; the mock planner keeps using `researcher` and `analyst`.

## Enable

Start commands from the repository root, same as `verticals/example`.

Agents: `verticals/ops_incident/agents/agents.yaml` is merged at startup with every other `verticals/*/agents/agents.yaml`. Core agents still come from `PROAGENT_AGENTS_CONFIG` (default `./config/agents.yaml`). There is no second agents env var. Sorted path order places this pack after `example` and before `support_desk`.

Skills: `./verticals/ops_incident/skills` is already on the default `PROAGENT_SKILLS_DIRS`, after `./skills`, `./verticals/example/skills`, and `./verticals/support_desk/skills`. Set the variable when you want this pack without editing the default:

```bash
export PROAGENT_SKILLS_DIRS=./skills,./verticals/ops_incident/skills
export PROAGENT_AGENTS_CONFIG=./config/agents.yaml
export PROAGENT_LLM_PROVIDER=mock
```

Restart the API and the worker after changing either value. Docker Compose sets the same skills list.

## Demo (mock LLM, no API key)

```bash
python scripts/demo_ops_incident.py
```

The script writes `data/demo_ops_incident.db` (gitignored) and deletes that file first. It uses the mock provider when `PROAGENT_LLM_PROVIDER` is unset.

You should see:

1. Tenant **Harborline On-Call**, API key `pk_demo_harborline_oncall`.
2. Three fixtures ingested (`api_5xx_runbook`, `database_failover`, `pager_severity`).
3. Search for `elevated API 5xx error rate` ranks `api_5xx_runbook` first.
4. `incident_triage` labels an elevated 5xx page `sev2` / `api` and cites **API 5xx Runbook** (the 2 percent line). `runbook_lookup` quotes that same runbook.
5. A mock task completes. The mock planner's steps are `researcher` then `analyst`, not `incident_commander`.
6. A `data_source` monitor proposes **Page on-call: API 5xx runbook matches**. Approve uses the default `noop` executor, so the status becomes `executed`. That writes an audit row and does not page anyone or call the network.

Ids change every run. The titles above do not.

Serve that database if you want the console:

```bash
export PROAGENT_ADMIN_KEY=dev-admin-key
export PROAGENT_LLM_PROVIDER=mock
export PROAGENT_DB_PATH=./data/demo_ops_incident.db
uvicorn proagent_os.main:app --host 0.0.0.0 --port 8080
```

Paste `pk_demo_harborline_oncall` at http://localhost:8080.

## What the fixtures cover

| File | Use it for |
|------|------------|
| `fixtures/api_5xx_runbook.md` | Elevated API 5xx as sev2, escalate to sev1 after a failed rollback |
| `fixtures/pager_severity.md` | Sev1, sev2, and sev3 paging rules |
| `fixtures/database_failover.md` | Primary unreachable, replica healthy, human announcement if data was lost |

Keyword triage misses paraphrases the fixtures do not contain. Treat a miss as "ask a human", which is what `needs_human` does when no passage matches. A sev1 page also sets `needs_human` even when a runbook matches.
