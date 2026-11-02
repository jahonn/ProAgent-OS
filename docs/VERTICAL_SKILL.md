# Add a vertical skill pack

A vertical pack is a directory directly under `verticals/`. The API and the worker merge its agent file at startup. Its skills load when that pack's `skills/` directory is listed in `PROAGENT_SKILLS_DIRS`. Core code stays as it is.

Copy `verticals/example/` (`market_scout` and `market_scan`). `verticals/support_desk/` and `verticals/ops_incident/` are the fuller packs: policies or runbooks, two skills, and an offline demo. The top-level `skills/` directory is the core pack (`research`, `analyst`). It is already on the default skills list. A new vertical lives under `verticals/<name>/`, beside `example`, `ops_incident`, and `support_desk`.

Run the commands below from the repository root.

## 1. Copy `verticals/example/`

```
verticals/<name>/
├── agents/agents.yaml
└── skills/<skill_id>/
    ├── SKILL.md
    └── run.py          # optional
```

`<name>` is one directory under `verticals/`. Startup globs `verticals/*/agents/agents.yaml` only, so a pack nested another level down is skipped. `README.md`, `fixtures/`, and helper modules are yours. The harness reads the agent YAML and each `skills/<skill_id>/SKILL.md` (and `run.py` when that file defines `run`).

### Agent file

`verticals/example/agents/agents.yaml`:

```yaml
agents:
  - id: market_scout
    role: specialist
    description: Example vertical specialist for market scanning
    tools: [kb_search, http_fetch, emit_recommendation, clock]
    system_prompt: |
      You are Market Scout (example vertical). Scan knowledge for market signals
      and emit recommendations when thresholds are crossed.
```

This file is real YAML (`yaml.safe_load`). Each entry needs `id`. `role` defaults to that id. `tools` defaults to an empty list. The prompt field is `system_prompt` (`system_prompt_template` is accepted as an alias). `description` is optional.

`role` is returned by `GET /api/v1/agents`. The planner selects an agent by `id` (`steps[].agent`), not by `role`.

Builtin tool names the orchestrator can offer: `kb_search`, `http_fetch`, `file_read`, `clock`, `emit_recommendation`. A name that is not registered is stored on the agent and returned by the API, and it is omitted from the model's tool list. `file_read` opens files under `fixtures/`, `data/`, or `skills/` (resolved paths). It does not open `verticals/`. The support-desk and ops-incident runners read their own fixtures with `pathlib`.

Pick a new `id`. Core agents load first from `PROAGENT_AGENTS_CONFIG` (default `./config/agents.yaml`): `planner`, `researcher`, `analyst`, `aggregator`. A vertical entry that reuses one of those ids replaces the core spec. Vertical files are applied in sorted path order, so `verticals/example` is merged before `verticals/ops_incident`, which is merged before `verticals/support_desk`. A later pack replaces an earlier pack's agent with the same id.

### `SKILL.md`

`verticals/example/skills/market_scan/SKILL.md`:

```markdown
---
id: market_scan
name: Market Scan
description: Example vertical skill — scan KB for market-related signals
---

# Market Scan

Look for mentions of price, demand, competitor, and trend.
Summarize as: signal, confidence, suggested action.
```

The front matter is line-based, not a YAML parser. The file starts with `---`, then one `key: value` line per field, then a closing `---`. The first colon separates the key and the value. One layer of surrounding quotes is stripped from the value. `id` defaults to the directory name, `name` to the title-cased directory name, and `description` to the first line of the body. Other keys are kept on `metadata` and returned by `GET /api/v1/skills/{id}`.

`PROAGENT_SKILLS_DIRS` points at the parent of the skill directories (`verticals/example/skills`), not at `verticals/example/skills/market_scan`. The loader only inspects immediate child directories for `SKILL.md`. Pointing it at the skill directory itself loads nothing.

### Optional `run.py`

`verticals/example/skills/market_scan/run.py`:

```python
from __future__ import annotations

from typing import Any, Dict, List


def run(tenant_id: str = "", signals: List[str] | None = None, **_: Any) -> Dict[str, Any]:
    signals = signals or ["demand", "price", "competitor"]
    return {
        "tenant_id": tenant_id,
        "watched_signals": signals,
        "note": "Stub vertical skill — wire to real data sources in your pack.",
    }
```

Keep `tenant_id`, your own keyword arguments, and a catch-all (`**_`). `run` may be a normal function or an async function. Invoke awaits the result when it has `__await__`.

HTTP invoke always passes `tenant_id` from the bearer, then every key inside the JSON `arguments` object. Leave `tenant_id` out of `arguments`. A second `tenant_id` raises `TypeError` (`got multiple values for keyword argument 'tenant_id'`). An unexpected keyword without `**kwargs` does the same.

When `SKILL.md` is present and there is no callable `run`, `POST /api/v1/skills/{id}/invoke` returns the prompt text and `"note": "no run.py; returned prompt only"`. `skills/research` is that case. `skills/analyst`, `market_scan`, `ticket_triage`, `policy_lookup`, `incident_triage`, and `runbook_lookup` each define `run`.

## 2. How startup merges agents

`AppServices` builds the registry once, for the API (`uvicorn proagent_os.main:app`) and for the worker (`python -m proagent_os.worker`):

1. `AgentRegistry(Path(settings.agents_config))` loads `PROAGENT_AGENTS_CONFIG`.
2. `merge_tree(Path("verticals"))` loads every file matching `verticals/*/agents/agents.yaml`.

`Path("verticals")` is relative to the process working directory, not the installed package. Start both processes from the repository root. If that directory is missing, the merge returns and startup continues with the core agents. `PROAGENT_AGENTS_CONFIG` stays the core file. Packs do not get their own agents variable.

With this tree, `GET /api/v1/agents` returns, in order: `planner`, `researcher`, `analyst`, `aggregator`, `market_scout`, `incident_commander`, `support_triage`. `incident_commander` comes from `verticals/ops_incident/agents/agents.yaml`. The glob is the same one that loads `market_scout` and `support_triage`.

`get_settings()` and `get_services()` are cached for the life of the process. Restart the API and the worker after adding or editing a pack.

## 3. Append the skills directory to `PROAGENT_SKILLS_DIRS`

`SkillLoader` reads the comma-separated paths in `PROAGENT_SKILLS_DIRS`. It does not walk `verticals/` on its own. Agents merge from the tree; skills do not.

The code default, used when the variable is unset in the environment and in `.env`, is:

```
./skills,./verticals/example/skills,./verticals/support_desk/skills,./verticals/ops_incident/skills
```

`example`, `support_desk`, and `ops_incident` are already on that list. A new pack is not. Set the full list, including the directories you still want:

```bash
export PROAGENT_SKILLS_DIRS=./skills,./verticals/example/skills,./verticals/support_desk/skills,./verticals/ops_incident/skills,./verticals/<name>/skills
export PROAGENT_AGENTS_CONFIG=./config/agents.yaml
export PROAGENT_LLM_PROVIDER=mock
```

Paths are relative to the working directory. A missing directory is skipped. Inside each directory, child folders load in sorted name order. The same skill `id` later in the list replaces the earlier skill (name, body, and `run`).

On a fresh checkout the loaded ids are `analyst`, `research`, `market_scan`, `policy_lookup`, `ticket_triage`, `incident_triage`, `runbook_lookup`. The specialist system prompt includes only the first five (`SkillLoader.list()[:5]`), and each body is truncated to 2000 characters. `incident_triage` and `runbook_lookup` still appear on `GET /api/v1/skills` and can be invoked. Put a directory earlier in `PROAGENT_SKILLS_DIRS` when the model should see that text.

A copied `.env` overrides the code default. Edit that file's `PROAGENT_SKILLS_DIRS` line, and leave `.env` uncommitted. Docker Compose sets the same four directories as literals on `api` and `worker` (`/app/skills`, `/app/verticals/example/skills`, `/app/verticals/support_desk/skills`, `/app/verticals/ops_incident/skills`). A host `export` does not change those literals. Add the new path in `docker-compose.yml` and rebuild. Agent merge still happens in the image when the process cwd is `/app` and the pack was copied in. Skills still need the environment variable.

Restart the API and the worker after the change.

## 4. Smoke-test with the mock LLM

`PROAGENT_LLM_PROVIDER=mock` needs no API key. The mock planner always returns `researcher` then `analyst`. It does not put `market_scout`, `incident_commander`, or `support_triage` on the plan. With `PROAGENT_LLM_PROVIDER=openai` and a non-empty `PROAGENT_LLM_API_KEY`, the planner prompt includes the merged id list (`Available agents: ...`) and the model can select a vertical agent. An empty key falls back to the mock planner.

The mock specialist treats a system prompt containing `market scout` like the researcher. The mock planner never selects that agent, so a smoke test of the pack checks the registry and `run`, then checks that a mock task still completes with the fixed plan.

### pytest

`tests/conftest.py` forces `PROAGENT_LLM_PROVIDER=mock` and points skills at `skills/`, `verticals/example/skills`, `verticals/support_desk/skills`, and `verticals/ops_incident/skills`.

```bash
pytest -q tests/test_support_desk.py tests/test_ops_incident.py
```

`test_support_desk_agent_and_skills_load` merges `verticals/*/agents/agents.yaml`, expects `support_triage` and `market_scout`, and expects runners for `ticket_triage`, `policy_lookup`, and `market_scan`. The other tests in that file ingest the three policy fixtures, run a mock task (plan `researcher`, `analyst`), and approve a follow-up through the default noop executor.

`test_ops_incident_agent_and_skills_load` expects `incident_commander` between `market_scout` and `support_triage`, and runners for `incident_triage` and `runbook_lookup`. `test_demo_ops_incident_script_under_mock` runs `scripts/demo_ops_incident.py` with `PROAGENT_LLM_PROVIDER=mock`.

CI runs `pytest -q` with `PROAGENT_LLM_PROVIDER=mock`.

### Tiny script

From the repository root, after `pip install -e .`. This checks the packs already in the tree. Add your skills directory to the list and assert your own id and `run` result the same way. The database path is under `/tmp`, so the script leaves `data/proagent.db` alone.

```python
"""Confirm the shipped vertical packs load. No API key. Run from the repo root."""
import asyncio
import os
from pathlib import Path

os.environ["PROAGENT_LLM_PROVIDER"] = "mock"
os.environ["PROAGENT_DB_PATH"] = "/tmp/proagent_vertical_smoke.db"
os.environ["PROAGENT_AGENTS_CONFIG"] = "config/agents.yaml"
os.environ["PROAGENT_SKILLS_DIRS"] = ",".join(
    [
        "skills",
        "verticals/example/skills",
        "verticals/support_desk/skills",
        "verticals/ops_incident/skills",
        # "verticals/<name>/skills",
    ]
)

from proagent_os.config import get_settings
from proagent_os.db import get_db, new_id, reset_db, utcnow
from proagent_os.llm.factory import reset_llm_cache
from proagent_os.services import reset_services

get_settings.cache_clear()
reset_llm_cache()
reset_db(Path(os.environ["PROAGENT_DB_PATH"]))
svc = reset_services()

agent_ids = [a.id for a in svc.agents.list()]
skill_ids = [s.id for s in svc.skills.list()]
assert agent_ids == [
    "planner",
    "researcher",
    "analyst",
    "aggregator",
    "market_scout",
    "incident_commander",
    "support_triage",
]
assert skill_ids == [
    "analyst",
    "research",
    "market_scan",
    "policy_lookup",
    "ticket_triage",
    "incident_triage",
    "runbook_lookup",
]

scan = svc.skills.get("market_scan")
assert scan is not None and scan.runner is not None
result = scan.runner(tenant_id="ten_smoke", signals=["demand"])
assert result == {
    "tenant_id": "ten_smoke",
    "watched_signals": ["demand"],
    "note": "Stub vertical skill — wire to real data sources in your pack.",
}

# conversations.tenant_id references tenants(id). Insert a row before run_task.
tid = new_id("ten")
get_db().insert(
    "tenants",
    {
        "id": tid,
        "name": "Vertical smoke",
        "api_key": "pk_vertical_smoke",
        "goals": "",
        "constraints": "",
        "tone": "concise",
        "created_at": utcnow(),
    },
)
task = asyncio.run(
    svc.orchestrator.run_task(tid, "Scan the knowledge base for demand signals.")
)
assert task["status"] == "completed"
assert [step["agent"] for step in task["plan"]["steps"]] == ["researcher", "analyst"]
print("agents:", agent_ids)
print("skills:", skill_ids)
print("mock plan:", [step["agent"] for step in task["plan"]["steps"]])
```

Set those environment variables before importing `proagent_os`. `get_settings()` reads them once and caches the result.

### HTTP invoke

Start the API from the repository root with `PROAGENT_LLM_PROVIDER=mock` and the skills list above. `market_scan` does not touch the database, so the admin key is enough:

```bash
curl -s -X POST http://localhost:8080/api/v1/skills/market_scan/invoke \
  -H "Authorization: Bearer ${PROAGENT_ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"arguments":{"signals":["price"]}}'
```

With `PROAGENT_ADMIN_KEY=dev-admin-key` the body is:

```json
{"skill_id":"market_scan","result":{"tenant_id":"admin","watched_signals":["price"],"note":"Stub vertical skill — wire to real data sources in your pack."}}
```

`tenant_id` is `admin` because the bearer is the admin key. A tenant API key uses that tenant's id instead. The JSON object is `{"arguments": { ... }}`. A body of `{"signals": ["price"]}` is accepted and those keys are ignored, so `market_scan` uses its default list `["demand", "price", "competitor"]`. An unknown skill id returns HTTP 404 `{"detail":"skill not found"}`. A missing bearer returns 401 `{"detail":"Missing Bearer token"}`.

`GET /api/v1/agents` and `GET /api/v1/skills` use the same bearer.

## 5. Richer example: `verticals/support_desk/`

Same layout, more files. Details and the expected demo transcript are in `verticals/support_desk/README.md`.

| Piece | Role |
| --- | --- |
| `agents/agents.yaml` | Agent `support_triage`. Tools: `kb_search`, `emit_recommendation`, `clock`. |
| `skills/ticket_triage/` | `SKILL.md` plus `run.py`. Keyword triage. It does not call a model. |
| `skills/policy_lookup/` | `SKILL.md` plus `run.py`. Quotes a matching policy passage. |
| `fixtures/*.md` | Three Northwind policies. `run.py` reads them when the caller omits `passages`. |
| `cite.py` | Helper imported by those runners. The harness does not load it. |
| `scripts/demo_support_desk.py` | Tenant, ingest, search, both skills, mock task, noop follow-up. |
| `tests/test_support_desk.py` | The checks in the pytest section above. |

```bash
python scripts/demo_support_desk.py
```

The script sets `PROAGENT_LLM_PROVIDER=mock` when that variable is unset, writes gitignored `data/demo_support_desk.db`, and deletes that file first. It exits if `support_triage` was not merged or if `ticket_triage` / `policy_lookup` failed to load. The printed mock plan is `researcher` then `analyst`. A real model can select `support_triage`; the mock planner does not.

The pack is an MVP demo. Approve marks the follow-up `executed` through the noop executor and does not send a customer reply.

## 6. Second pack: `verticals/ops_incident/`

Same layout as support desk, for an on-call queue. Details and the expected demo transcript are in `verticals/ops_incident/README.md`.

| Piece | Role |
| --- | --- |
| `agents/agents.yaml` | Agent `incident_commander`. Tools: `kb_search`, `emit_recommendation`, `clock`. |
| `skills/incident_triage/` | `SKILL.md` plus `run.py`. Keyword severity and service. It does not call a model. |
| `skills/runbook_lookup/` | `SKILL.md` plus `run.py`. Quotes a matching runbook passage. |
| `fixtures/*.md` | Three Harborline runbooks. `run.py` reads them when the caller omits `passages`. |
| `cite.py` | Helper imported by those runners. The harness does not load it. |
| `scripts/demo_ops_incident.py` | Tenant, ingest, search, both skills, mock task, noop page follow-up. |
| `tests/test_ops_incident.py` | Registry merge, keyword citations, ingest path, and the demo script under mock. |

```bash
python scripts/demo_ops_incident.py
```

The script sets `PROAGENT_LLM_PROVIDER=mock` when that variable is unset, writes gitignored `data/demo_ops_incident.db`, and deletes that file first. It exits if `incident_commander` was not merged or if `incident_triage` / `runbook_lookup` failed to load. The printed mock plan is `researcher` then `analyst`. A real model can select `incident_commander`; the mock planner does not.

The pack is an MVP demo. Approve marks the page follow-up `executed` through the noop executor and does not page anyone. A sev1 page sets `needs_human` on `incident_triage` even when a runbook matches.
