"""Plan-and-replan outer loop helpers (stub).

Industry mapping:

* LangGraph plan-and-execute — after executing steps, optionally replan when
  results are empty / failed / insufficient
  (https://www.langchain.com/blog/planning-agents)
* Plan-and-Solve (Wang et al., 2023) — the planner can revise the plan
  (https://arxiv.org/abs/2305.04091)

The ReAct-style specialist tool loop stays the inner ring. This module only
decides *when* the outer planner should run again.

**Stub:** heuristic triggers + env knobs. Not an LLM sufficiency judge, not
DAG replanning, not cost budgets. See docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


def _steps_of(plan: Optional[Mapping[str, Any]]) -> List[Any]:
    if not isinstance(plan, Mapping):
        return []
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return []
    return steps


def _result_text(step: Mapping[str, Any]) -> str:
    return str(step.get("result") or "").strip()


def step_looks_failed(step: Mapping[str, Any]) -> bool:
    """Heuristic failure marker for one specialist step (stub, not an LLM judge)."""
    if step.get("failed") is True:
        return True
    text = _result_text(step)
    if not text:
        return True
    lower = text.lower()
    if lower.startswith("[error]") or lower.startswith("error:"):
        return True
    if "all tool calls failed" in lower:
        return True
    return False


def observations_summary(
    plan: Optional[Mapping[str, Any]],
    step_results: Sequence[Mapping[str, Any]],
    *,
    limit: int = 4000,
) -> str:
    """Compact prior-plan + step observations for a replan planner prompt."""
    payload = {
        "prior_plan": plan if isinstance(plan, Mapping) else {},
        "step_results": [
            {
                "agent": s.get("agent"),
                "goal": s.get("goal"),
                "result": _result_text(s)[:800],
                "failed": step_looks_failed(s),
            }
            for s in step_results
        ],
    }
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        raw = str(payload)
    return raw if len(raw) <= limit else raw[: limit - 3] + "..."


def should_replan(
    plan: Optional[Mapping[str, Any]],
    step_results: Sequence[Mapping[str, Any]],
    *,
    enabled: bool,
    attempt: int,
    max_replans: int,
    force: bool = False,
) -> Tuple[bool, str]:
    """Decide whether to call the planner again.

    ``attempt`` is how many replans have already completed (0 = first chance).
    ``force`` is for fixtures / demos: triggers while ``attempt < max_replans``.

    Returns ``(do_replan, reason)``.
    """
    if not enabled:
        return False, "disabled"
    if max_replans <= 0:
        return False, "max_zero"
    if attempt >= max_replans:
        return False, "max_reached"
    if force:
        return True, "force"

    steps = _steps_of(plan)
    if not steps:
        return True, "empty_plan"
    if not step_results:
        return True, "empty_step_results"
    if all(step_looks_failed(s) for s in step_results):
        # Distinguishes blank aggregate inputs from explicit failures when possible.
        if all(not _result_text(s) for s in step_results):
            return True, "empty_aggregate_inputs"
        return True, "all_steps_failed"
    return False, "sufficient"
