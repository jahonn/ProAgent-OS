#!/usr/bin/env python3
"""Ops incident demo: tenant, runbook ingest, search, triage skill, mock task, page follow-up.

Offline. No API key. Approve uses the default noop executor (status executed, no network, no page).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PROAGENT_DB_PATH", str(ROOT / "data" / "demo_ops_incident.db"))
os.environ.setdefault("PROAGENT_ADMIN_KEY", "dev-admin-key")
os.environ.setdefault("PROAGENT_LLM_PROVIDER", "mock")
os.environ.setdefault("PROAGENT_EXECUTOR_MODE", "noop")
os.environ.setdefault("PROAGENT_EXECUTOR_ON_APPROVE", "true")
os.environ.setdefault("PROAGENT_AGENTS_CONFIG", str(ROOT / "config" / "agents.yaml"))


def _ensure_ops_skills() -> None:
    """Keep any caller-provided dirs and always include this pack."""
    extra = str(ROOT / "verticals" / "ops_incident" / "skills")
    current = os.environ.get("PROAGENT_SKILLS_DIRS", "")
    if current.strip():
        parts = [p.strip() for p in current.split(",") if p.strip()]
    else:
        parts = [
            str(ROOT / "skills"),
            str(ROOT / "verticals" / "example" / "skills"),
            str(ROOT / "verticals" / "support_desk" / "skills"),
        ]
    if extra not in parts:
        parts.append(extra)
    os.environ["PROAGENT_SKILLS_DIRS"] = ",".join(parts)


_ensure_ops_skills()

FIXTURES = ROOT / "verticals" / "ops_incident" / "fixtures"
API_PAGE = (
    "Subject: Elevated API errors\n"
    "Checkout API 5xx error rate is above 5 percent for 10 minutes in us-east-1."
)


async def main() -> None:
    from proagent_os.config import get_settings
    from proagent_os.db import get_db, new_id, reset_db, utcnow
    from proagent_os.llm.factory import reset_llm_cache
    from proagent_os.services import reset_services

    get_settings.cache_clear()
    reset_llm_cache()
    db_path = Path(os.environ["PROAGENT_DB_PATH"])
    if db_path.exists():
        db_path.unlink()
    reset_db(db_path)
    svc = reset_services()

    print("=== ProAgent OS Ops Incident Demo ===\n")

    agent_ids = [a.id for a in svc.agents.list()]
    skill_ids = [s.id for s in svc.skills.list()]
    if "incident_commander" not in agent_ids:
        raise SystemExit(f"incident_commander was not merged from verticals/*/agents. agents={agent_ids}")
    if "incident_triage" not in skill_ids or "runbook_lookup" not in skill_ids:
        raise SystemExit(f"ops incident skills missing. skills={skill_ids}")
    print(f"agents: {agent_ids}")
    print(f"skills: {skill_ids}\n")

    tid = new_id("ten")
    api_key = "pk_demo_harborline_oncall"
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Harborline On-Call",
            "api_key": api_key,
            "goals": "Answer from published runbooks and escalate sev1 pages",
            "constraints": "Do not invent severity rules or page anyone without approval",
            "tone": "concise and cite the runbook",
            "created_at": utcnow(),
        },
    )
    print(f"[1] Tenant created: id={tid} api_key={api_key}")

    docs = []
    for path in sorted(FIXTURES.glob("*.md")):
        docs.append(svc.kb.ingest_file(tid, path))
    print("[2] Ingested runbooks: " + ", ".join(f"{d['title']} ({d['chunks']} chunks)" for d in docs))
    if len(docs) != 3:
        raise SystemExit(f"expected 3 fixtures, ingested {len(docs)}")

    hits = svc.kb.search(tid, "elevated API 5xx error rate", top_k=3)
    if not hits or hits[0]["title"] != "api_5xx_runbook":
        raise SystemExit(f"expected api_5xx_runbook as the top hit, got {hits[:1]}")
    print(
        f"    Search 'elevated API 5xx error rate' → {len(hits)} hits; "
        f"top={hits[0]['title']} score={hits[0]['score']}"
    )

    triage = svc.skills.get("incident_triage").runner(tenant_id=tid, page=API_PAGE, passages=hits)
    lookup = svc.skills.get("runbook_lookup").runner(
        tenant_id=tid, query="elevated API 5xx error rate", passages=hits
    )
    if triage["severity"] != "sev2" or triage["service"] != "api":
        raise SystemExit(f"unexpected triage labels: {triage}")
    if not triage["citations"] or triage["citations"][0]["title"] != "API 5xx Runbook":
        raise SystemExit(f"expected API 5xx Runbook citation, got {triage['citations']}")
    if not lookup["citations"] or lookup["citations"][0]["title"] != "API 5xx Runbook":
        raise SystemExit(f"expected runbook lookup to cite API 5xx Runbook, got {lookup['citations']}")
    print(
        f"[3] incident_triage → {triage['severity']} / {triage['service']}; "
        f"cite {triage['citations'][0]['title']}"
    )
    print(f"    {triage['citations'][0]['excerpt']}")
    print(f"    follow_up: {triage['follow_up']}")
    print(f"    runbook_lookup → {lookup['citations'][0]['title']}")

    task = await svc.orchestrator.run_task(
        tid,
        "Checkout API 5xx error rate is elevated. Which runbook should on-call follow?",
    )
    if task["status"] != "completed":
        raise SystemExit(f"task did not complete: {task['status']}")
    steps = [s.get("agent") for s in task["plan"].get("steps", [])]
    print(f"[4] Mock task {task['task_id']} status={task['status']} plan={steps}")
    print("    Mock planner assigns researcher then analyst. incident_commander is registered for a real model.")

    mon = svc.monitors.create(
        tid,
        name="API 5xx page",
        kind="data_source",
        config={
            "query": "elevated API 5xx error rate",
            "title": "Page on-call: API 5xx runbook matches",
        },
        interval_seconds=3600,
    )
    run = await svc.monitors.run_once(tid, mon["id"])
    rec = run.get("recommendation") or {}
    if rec.get("title") != "Page on-call: API 5xx runbook matches":
        raise SystemExit(f"monitor did not propose the page: {run}")
    print(f"[5] Monitor {mon['id']} proposed {rec['id']}: {rec['title']} ({rec['status']})")

    approved = svc.recommendations.decide(tid, rec["id"], "approved")
    execution = approved.get("execution") or {}
    if approved.get("status") != "executed" or execution.get("mode") != "noop" or execution.get("ok") is not True:
        raise SystemExit(f"expected noop executor to mark executed, got {approved}")
    print(
        f"[6] Recommendation status → {approved['status']}"
        f" (executor {execution.get('mode')}, ok={execution.get('ok')}; nobody is paged)"
    )

    outcome = svc.metrics.record_outcome(tid, rec["id"], "pages_reviewed", 1, {"note": "demo only"})
    learning = svc.learning.apply_outcome_feedback(
        tid, rec["id"], "pages_reviewed", 1, skill_id="incident_triage", success_threshold=0.0
    )
    print(f"    Outcome {outcome['metric_name']}={outcome['value']}; skill weight {learning['skill_weight']}")
    print(
        "\nDemo complete. Keyword triage. Noop executor marks the page executed"
        " and does not notify anyone. Not a production incident system."
    )
    print(f"Use Authorization: Bearer {api_key}")


if __name__ == "__main__":
    asyncio.run(main())
