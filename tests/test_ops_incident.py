"""Ops incident vertical: registry merge, keyword skills, and the offline demo path."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from proagent_os.db import get_db, new_id, utcnow
from proagent_os.harness.registry import AgentRegistry
from proagent_os.services import get_services

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path("verticals/ops_incident/fixtures")
API_PAGE = (
    "Subject: Elevated API errors\n"
    "Checkout API 5xx error rate is above 5 percent for 10 minutes in us-east-1."
)


def _tenant() -> str:
    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Harborline On-Call",
            "api_key": new_id("pk"),
            "goals": "Answer from published runbooks",
            "constraints": "Do not invent severity rules",
            "tone": "concise",
            "created_at": utcnow(),
        },
    )
    return tid


def test_ops_incident_agent_and_skills_load():
    reg = AgentRegistry(Path("config/agents.yaml"))
    reg.merge_tree(Path("verticals"))
    merged = reg.get("incident_commander")
    assert merged is not None
    assert merged.role == "specialist"
    assert merged.tools == ["kb_search", "emit_recommendation", "clock"]
    assert "runbook" in merged.system_prompt.lower()
    ids = [agent.id for agent in reg.list()]
    assert ids.index("market_scout") < ids.index("incident_commander") < ids.index("support_triage")

    svc = get_services()
    agent = svc.agents.get("incident_commander")
    assert agent is not None and agent.tools == merged.tools
    triage = svc.skills.get("incident_triage")
    lookup = svc.skills.get("runbook_lookup")
    assert triage is not None and triage.runner is not None
    assert lookup is not None and lookup.runner is not None
    loaded = [skill.id for skill in svc.skills.list()]
    assert loaded.index("ticket_triage") < loaded.index("incident_triage")
    assert loaded.index("incident_triage") < loaded.index("runbook_lookup")
    assert len(list(FIXTURES.glob("*.md"))) == 3


def test_incident_triage_cites_runbooks_and_honors_passages():
    triage = get_services().skills.get("incident_triage")
    lookup = get_services().skills.get("runbook_lookup")
    assert triage is not None and triage.runner is not None
    assert lookup is not None and lookup.runner is not None

    api = triage.runner(tenant_id="ten_demo", page=API_PAGE)
    assert api["severity"] == "sev2"
    assert api["service"] == "api"
    assert api["needs_human"] is False
    assert api["citations"][0]["title"] == "API 5xx Runbook"
    assert "2 percent" in api["citations"][0]["excerpt"]
    assert "API 5xx Runbook" in api["follow_up"]

    outage = triage.runner(
        page="Complete outage: checkout API is down for all customers. Possible customer data exposure."
    )
    assert outage["service"] == "api"
    assert outage["severity"] == "sev1"
    assert outage["needs_human"] is True
    assert outage["citations"][0]["title"] == "API 5xx Runbook"

    failover = triage.runner(page="Primary database is unreachable. Replica is healthy. Need a failover.")
    assert failover["service"] == "database"
    assert failover["severity"] == "sev2"
    assert failover["needs_human"] is False
    assert failover["citations"][0]["title"] == "Database Failover"

    unknown = triage.runner(page="Where is the office snack menu?")
    assert unknown["service"] == "general"
    assert unknown["severity"] == "sev3"
    assert unknown["citations"] == []
    assert unknown["needs_human"] is True

    blocked = triage.runner(
        page="API 5xx error rate is elevated",
        passages=[{"title": "Lunch", "source": "lunch.md", "content": "The office serves soup on Friday."}],
    )
    assert blocked["service"] == "api"
    assert blocked["severity"] == "sev2"
    assert blocked["citations"] == []
    assert blocked["needs_human"] is True

    quoted = lookup.runner(query="primary database failover replica")
    assert quoted["citations"][0]["title"] == "Database Failover"
    assert "replica" in quoted["citations"][0]["excerpt"].lower()
    assert lookup.runner(query="office snack menu")["citations"] == []
    assert lookup.runner(query="")["citations"] == []


@pytest.mark.asyncio
async def test_ops_incident_ingest_search_task_and_page():
    tid = _tenant()
    svc = get_services()
    paths = sorted(FIXTURES.glob("*.md"))
    assert [p.name for p in paths] == [
        "api_5xx_runbook.md",
        "database_failover.md",
        "pager_severity.md",
    ]
    for path in paths:
        meta = svc.kb.ingest_file(tid, path)
        assert meta["chunks"] >= 1
        assert meta["title"] == path.stem

    hits = svc.kb.search(tid, "elevated API 5xx error rate", top_k=3)
    assert hits
    assert hits[0]["title"] == "api_5xx_runbook"
    assert "5xx" in hits[0]["content"]

    triage = svc.skills.get("incident_triage").runner(tenant_id=tid, page=API_PAGE, passages=hits)
    assert triage["citations"][0]["title"] == "API 5xx Runbook"
    assert "2 percent" in triage["citations"][0]["excerpt"]

    task = await svc.orchestrator.run_task(
        tid,
        "Checkout API 5xx error rate is elevated. Which runbook should on-call follow?",
    )
    assert task["status"] == "completed"
    assert task["result"]
    assert [step.get("agent") for step in task["plan"]["steps"]] == ["researcher", "analyst"]

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
    rec = run["recommendation"]
    assert rec["status"] == "pending"
    assert rec["title"] == "Page on-call: API 5xx runbook matches"
    assert rec["action_payload"]["type"] == "follow_up"
    assert "5xx" in rec["rationale"]
    decided = svc.recommendations.decide(tid, rec["id"], "approved")
    assert decided["status"] == "executed"
    assert decided["execution"] == {"ok": True, "mode": "noop"}


def test_demo_ops_incident_script_under_mock(tmp_path):
    env = os.environ.copy()
    env["PROAGENT_LLM_PROVIDER"] = "mock"
    env["PROAGENT_LLM_API_KEY"] = ""
    env["PROAGENT_DB_PATH"] = str(tmp_path / "demo_ops_incident.db")
    env["PROAGENT_EXECUTOR_MODE"] = "noop"
    env["PROAGENT_EXECUTOR_ON_APPROVE"] = "true"
    env["PROAGENT_AGENTS_CONFIG"] = str((ROOT / "config" / "agents.yaml").resolve())
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_ops_incident.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "incident_commander" in proc.stdout
    assert "API 5xx Runbook" in proc.stdout
    assert "['researcher', 'analyst']" in proc.stdout
    assert "Page on-call: API 5xx runbook matches" in proc.stdout
    assert "executor noop, ok=True" in proc.stdout
    assert "Not a production incident system." in proc.stdout
