#!/usr/bin/env python3
"""
Run a task with whatever LLM provider is configured (mock or openai).
Prints specialist tool-round traces.

Env (examples):
  PROAGENT_LLM_PROVIDER=mock
  # or:
  PROAGENT_LLM_PROVIDER=openai
  PROAGENT_LLM_API_KEY=sk-...
  PROAGENT_LLM_BASE_URL=https://api.openai.com/v1
  PROAGENT_LLM_MODEL=gpt-4o-mini
  PROAGENT_MAX_TOOL_ROUNDS=8
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PROAGENT_DB_PATH", str(ROOT / "data" / "demo_live.db"))
os.environ.setdefault("PROAGENT_ADMIN_KEY", "dev-admin-key")
os.environ.setdefault("PROAGENT_LLM_PROVIDER", "mock")
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
    settings = get_settings()

    print("=== ProAgent OS Live Tool Demo ===")
    print(f"provider={settings.llm_provider} model={settings.llm_model} max_tool_rounds={settings.max_tool_rounds}")
    if settings.llm_provider == "openai" and not settings.llm_api_key:
        print("WARNING: openai provider selected but PROAGENT_LLM_API_KEY empty — factory falls back to mock.")
    print()

    tid = new_id("ten")
    get_db().insert(
        "tenants",
        {
            "id": tid,
            "name": "Live Demo Co",
            "api_key": "pk_live_demo",
            "goals": "Raise conversion",
            "constraints": "No pricing changes",
            "tone": "concise",
            "created_at": utcnow(),
        },
    )
    await svc.kb.ingest_url(tid, "fixture://sample_product.html")
    svc.kb.ingest_file(tid, ROOT / "fixtures" / "sample_notes.md")

    result = await svc.orchestrator.run_task(
        tid,
        "Analyze conversion vs target and propose one concrete next action.",
    )

    print(f"task_id={result['task_id']} status={result['status']}")
    print(f"plan_steps={[s.get('agent') for s in result['plan'].get('steps', [])]}")
    print("\n--- Tool / specialist trace ---")
    for ev in result["trace"]:
        phase = ev.get("phase")
        if phase == "plan":
            print(f"[plan] model={ev.get('model')} steps={ev.get('parsed', {}).get('steps')}")
        elif phase == "specialist":
            print(
                f"[specialist] agent={ev.get('agent')} round={ev.get('round')} "
                f"model={ev.get('model')} tools={ev.get('tool_call_names')} "
                f"preview={(ev.get('content_preview') or '')[:120]!r}"
            )
        elif phase == "tool":
            print(
                f"  [tool] id={ev.get('id')} name={ev.get('name')} "
                f"ok={ev.get('ok')} latency_ms={ev.get('latency_ms')} "
                f"args={ev.get('args_summary')} err={ev.get('error')}"
            )
        elif phase == "aggregate":
            print(f"[aggregate] model={ev.get('model')}")
    print("\n--- Final result ---")
    print(result["result"][:1200])
    tool_n = sum(1 for e in result["trace"] if e.get("phase") == "tool")
    print(f"\nTool invocations: {tool_n}")


if __name__ == "__main__":
    asyncio.run(main())
