#!/usr/bin/env python3
"""End-to-end demo: tenant → ingest → task → monitor → approve → outcome."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PROAGENT_DB_PATH", str(ROOT / "data" / "demo.db"))
os.environ.setdefault("PROAGENT_ADMIN_KEY", "dev-admin-key")
os.environ.setdefault("PROAGENT_LLM_PROVIDER", "mock")
os.environ.setdefault("PROAGENT_EXECUTOR_MODE", "noop")
os.environ.setdefault("PROAGENT_EXECUTOR_ON_APPROVE", "true")
os.environ.setdefault("PROAGENT_AGENTS_CONFIG", str(ROOT / "config" / "agents.yaml"))
os.environ.setdefault(
    "PROAGENT_SKILLS_DIRS",
    ",".join(
        [
            str(ROOT / "skills"),
            str(ROOT / "verticals" / "example" / "skills"),
            str(ROOT / "verticals" / "support_desk" / "skills"),
            str(ROOT / "verticals" / "ops_incident" / "skills"),
        ]
    ),
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

    print("=== ProAgent OS Demo ===\n")

    # 1) Create tenant
    tid = new_id("ten")
    api_key = "pk_demo_acme_analytics"
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Acme Analytics",
            "api_key": api_key,
            "goals": "Raise trial-to-paid conversion to 2.5%",
            "constraints": "No pricing changes without finance approval",
            "tone": "confident and concise",
            "created_at": utcnow(),
        },
    )
    print(f"[1] Tenant created: id={tid} api_key={api_key}")

    # 2) Ingest fixture page + notes
    r1 = await svc.kb.ingest_url(tid, "fixture://sample_product.html")
    r2 = svc.kb.ingest_file(tid, ROOT / "fixtures" / "sample_notes.md")
    print(f"[2] Ingested KB: {r1['title']} ({r1['chunks']} chunks), {r2['title']} ({r2['chunks']} chunks)")

    hits = svc.kb.search(tid, "conversion rate", top_k=2)
    print(f"    Search 'conversion rate' → {len(hits)} hits; top score={hits[0]['score'] if hits else None}")

    # 3) Run task
    task = await svc.orchestrator.run_task(
        tid,
        "Analyze why conversion is below target and propose a next action.",
    )
    print(f"[3] Task {task['task_id']} status={task['status']}")
    print(f"    Plan steps: {[s.get('agent') for s in task['plan'].get('steps', [])]}")
    print(f"    Result preview:\n{task['result'][:400]}\n")

    # 4) Proactive monitor → recommendation
    mon = svc.monitors.create(
        tid,
        name="Conversion watch",
        kind="metric_stub",
        config={"metric": "conversion_rate", "current": 0.018, "target": 0.025, "title": "Conversion below 2.5% target"},
        interval_seconds=60,
    )
    run = await svc.monitors.run_once(tid, mon["id"])
    rec = run["recommendation"]
    print(f"[4] Monitor {mon['id']} emitted recommendation {rec['id']}: {rec['title']}")

    # 5) Approve (default noop executor marks it executed; no network)
    approved = svc.recommendations.decide(tid, rec["id"], "approved")
    execution = approved.get("execution") or {}
    print(
        f"[5] Recommendation status → {approved['status']}"
        f" (executor {execution.get('mode', 'off')}, ok={execution.get('ok')})"
    )

    # 6) Record outcome + learning
    outcome = svc.metrics.record_outcome(tid, rec["id"], "conversion_lift_pp", 0.4, {"note": "after onboarding fix"})
    learning = svc.learning.apply_outcome_feedback(
        tid, rec["id"], "conversion_lift_pp", 0.4, skill_id="market_scan", success_threshold=0.0
    )
    print(f"[6] Outcome recorded: {outcome}")
    print(f"    Learning: {json.dumps(learning)}")

    # Dashboard snapshot
    dash = {
        "tasks": get_db().fetchall("SELECT status, COUNT(*) n FROM tasks WHERE tenant_id=? GROUP BY status", (tid,)),
        "acceptance": svc.recommendations.acceptance_rate(tid),
        "outcomes": svc.metrics.summary(tid),
        "skills": [s.id for s in svc.skills.list()],
        "agents": [a.id for a in svc.agents.list()],
    }
    print("\n[Dashboard]")
    print(json.dumps(dash, indent=2, default=str))
    print("\nDemo complete. Start API with: uvicorn proagent_os.main:app --reload --port 8080")
    print(f"Use Authorization: Bearer {api_key}")


if __name__ == "__main__":
    asyncio.run(main())
