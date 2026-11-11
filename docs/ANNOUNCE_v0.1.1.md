# Announce draft — first public Release v0.1.1

Paste the post below into GitHub Discussions
([Announcements](https://github.com/jahonn/ProAgent-OS/discussions/categories/announcements)
or [Show and tell](https://github.com/jahonn/ProAgent-OS/discussions/categories/show-and-tell)).
This file is the draft. Publishing the post is a separate step.

---

# ProAgent OS v0.1.1

**Self-hosted harness for commercial agent products:** a planner–specialist loop, a private knowledge base, proactive human approval, and a small outcome loop, exposed as a multi-tenant FastAPI app.

First public Release: **[v0.1.1](https://github.com/jahonn/ProAgent-OS/releases/tag/v0.1.1)**.

Two offline mock demos. No API key. From a clone, after the install in the [README](https://github.com/jahonn/ProAgent-OS/blob/main/README.md#quick-start-mock-llm):

```bash
python scripts/demo.py               # product loop
python scripts/demo_ops_incident.py  # on-call runbooks
```

[`scripts/demo.py`](https://github.com/jahonn/ProAgent-OS/blob/main/scripts/demo.py) creates a tenant, ingests a small knowledge base, completes a task, and marks the monitor recommendation executed with the noop executor. [`scripts/demo_ops_incident.py`](https://github.com/jahonn/ProAgent-OS/blob/main/scripts/demo_ops_incident.py) ingests the Harborline runbooks, cites **API 5xx Runbook**, and approves **Page on-call: API 5xx runbook matches** through that same noop executor. Nobody is paged.

How the loop is shaped, including what is still a stub: [docs/ARCHITECTURE.md](https://github.com/jahonn/ProAgent-OS/blob/main/docs/ARCHITECTURE.md).

## What it is

- An early open-source MVP for demos, scaffolding vertical packs, and self-hosted experiments. License: MIT.
- Original design and code. An independent project, not affiliated with any third-party agent product.
- One service you run yourself: FastAPI `/api/v1`, a worker, a small console, and one SQLite file. The mock LLM path needs no API key. The same API can point at an OpenAI-compatible `/v1/chat/completions` endpoint.

## What it is not

- Production SaaS. This cut is for demos, vertical-pack scaffolding, and self-hosted experiments. Read the [roadmap (issue #2)](https://github.com/jahonn/ProAgent-OS/issues/2) before any production SaaS use.
- A hosted product. You run the FastAPI app and worker yourself. SQLite stays a single-writer file.
- An action runner. Recommendation inbox approve runs a local noop or one configured webhook. The action payload is not treated as a URL or shell command, and it does not run arbitrary action code. Task-run HITL resume is a separate path: `POST /api/v1/tasks/{id}/resume` can invoke a registered `requires_human_approval` tool once when `PROAGENT_HITL_EXECUTE_ON_APPROVE` is on (the default).

Human notes for the cut: [docs/RELEASE_NOTES_v0.1.1.md](https://github.com/jahonn/ProAgent-OS/blob/main/docs/RELEASE_NOTES_v0.1.1.md). Demo output to expect: [docs/DEMO.md](https://github.com/jahonn/ProAgent-OS/blob/main/docs/DEMO.md).
