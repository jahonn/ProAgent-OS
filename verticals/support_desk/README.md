# Support desk vertical

MVP pack for a private support knowledge base. It is a demo you can run offline. It is not a production helpdesk, and it does not send customer email.

What you get:

- Agent `support_triage` (concise, must cite the knowledge base, files a follow-up when a person should decide).
- Skills `ticket_triage` and `policy_lookup` (`SKILL.md` plus a deterministic `run.py`).
- Three short Northwind policies in `fixtures/`.
- `scripts/demo_support_desk.py` — tenant, ingest, search, skill, mock task, and a monitor recommendation.

`run.py` ranks keywords. It does not call a model. A real `PROAGENT_LLM_PROVIDER=openai` run can still select `support_triage`; the mock planner keeps using `researcher` and `analyst`.

## Enable

Start commands from the repository root, same as `verticals/example`.

Agents: `verticals/support_desk/agents/agents.yaml` is merged at startup with every other `verticals/*/agents/agents.yaml`. Core agents still come from `PROAGENT_AGENTS_CONFIG` (default `./config/agents.yaml`). There is no second agents env var.

Skills: `./verticals/support_desk/skills` is already on the default `PROAGENT_SKILLS_DIRS`, next to `./skills`, `./verticals/example/skills`, and `./verticals/ops_incident/skills`. Set the variable when you want this pack without editing the default:

```bash
export PROAGENT_SKILLS_DIRS=./skills,./verticals/support_desk/skills
export PROAGENT_AGENTS_CONFIG=./config/agents.yaml
export PROAGENT_LLM_PROVIDER=mock
```

Restart the API and the worker after changing either value. Docker Compose sets the same skills list.

## Demo (mock LLM, no API key)

```bash
python scripts/demo_support_desk.py
```

The script writes `data/demo_support_desk.db` (gitignored) and deletes that file first. It uses the mock provider when `PROAGENT_LLM_PROVIDER` is unset.

You should see:

1. Tenant **Northwind Support**, API key `pk_demo_northwind_support`.
2. Three fixtures ingested (`refund_policy`, `sla_first_response`, `account_access_faq`).
3. Search for `refund within 30 days` ranks `refund_policy` first.
4. `ticket_triage` labels a 10-day refund request `high` / `billing` and cites **Refund Policy** (the 30-day line). `policy_lookup` quotes that same policy.
5. A mock task completes. The mock planner's steps are `researcher` then `analyst`, not `support_triage`.
6. A `data_source` monitor proposes **Follow up: refund request matches the 30-day policy**. Approve uses the default `noop` executor, so the status becomes `executed`. That writes an audit row and does not call the network or send a customer reply.

Ids change every run. The titles above do not.

Serve that database if you want the console:

```bash
export PROAGENT_ADMIN_KEY=dev-admin-key
export PROAGENT_LLM_PROVIDER=mock
export PROAGENT_DB_PATH=./data/demo_support_desk.db
uvicorn proagent_os.main:app --host 0.0.0.0 --port 8080
```

Paste `pk_demo_northwind_support` at http://localhost:8080.

## What the fixtures cover

| File | Use it for |
|------|------------|
| `fixtures/refund_policy.md` | 30-day refunds, invoice id, human exception after the window |
| `fixtures/sla_first_response.md` | First-response targets for urgent, high, and normal |
| `fixtures/account_access_faq.md` | Password reset, lockout, backup codes, takeover |

Keyword triage misses paraphrases the fixtures do not contain. Treat a miss as "ask a human", which is what `needs_human` does when no passage matches.
