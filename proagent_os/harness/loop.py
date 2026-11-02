"""Typed agent-loop phases and trajectory helpers.

ProAgent OS outer loop is plan → specialist (inner tool rounds) → [optional replan] → aggregate.
That matches industry "agent harness" practice:

* OpenAI Agents SDK runner: model → tool calls → continue / final output
  (https://developers.openai.com/api/docs/guides/agents/running-agents)
* Anthropic client tool use: while stop_reason == tool_use, run tools, observe
  (https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)
* LangGraph plan-and-execute: plan steps, execute, then synthesize / replan
  (https://www.langchain.com/blog/planning-agents)

Trace rows already stored ``phase`` as a string. This module makes those values
stable constants and offers a thin trajectory view consumed by `proagent_os.eval`
(LangSmith / ADK-style tool-path checks) without changing the mock LLM path.

See docs/ARCHITECTURE.md. Trajectory helpers are a **stub**, not an LLM judge.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union


class LoopPhase(str, Enum):
    """Stable phase labels written into task ``trace_json`` rows."""

    PLAN = "plan"
    SPECIALIST = "specialist"
    TOOL = "tool"
    AGGREGATE = "aggregate"
    HITL = "hitl"
    COMPACTION = "compaction"
    REPLAN = "replan"


# Documented outer-loop order (specialist may emit many TOOL rows between).
OUTER_LOOP_PHASES: tuple[LoopPhase, ...] = (
    LoopPhase.PLAN,
    LoopPhase.SPECIALIST,
    LoopPhase.TOOL,
    LoopPhase.AGGREGATE,
)

PhaseLike = Union[LoopPhase, str]


def phase_value(phase: PhaseLike) -> str:
    """Normalize a phase enum or string to the wire value stored in traces."""
    if isinstance(phase, LoopPhase):
        return phase.value
    return str(phase)


def event(phase: PhaseLike, **fields: Any) -> Dict[str, Any]:
    """Build one trace event dict. Extra fields are kept for forward compat."""
    payload: Dict[str, Any] = {"phase": phase_value(phase)}
    payload.update(fields)
    return payload


def iter_phase(
    trace: Sequence[Mapping[str, Any]],
    phase: PhaseLike,
) -> Iterable[Mapping[str, Any]]:
    """Yield trace rows whose ``phase`` matches."""
    want = phase_value(phase)
    for row in trace:
        if row.get("phase") == want:
            yield row


def tool_trajectory(trace: Sequence[Mapping[str, Any]]) -> List[str]:
    """Ordered tool names from a task trace (trajectory-eval stub input).

    This is the same idea as LangSmith / Google ADK tool-trajectory checks:
    compare the actual tool path to an expected list. Scoring lives in proagent_os.eval (ordered_subset stub).
    """
    names: List[str] = []
    for row in iter_phase(trace, LoopPhase.TOOL):
        name = row.get("name")
        if name:
            names.append(str(name))
    return names


def phase_counts(trace: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Count events per phase label (unknown labels are preserved)."""
    counts: Dict[str, int] = {}
    for row in trace:
        label = str(row.get("phase") or "unknown")
        counts[label] = counts.get(label, 0) + 1
    return counts


def summarize_run(trace: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compact run summary for dashboards / future eval harness wiring.

    **Stub:** returns counts and the tool trajectory only. It does not call an
    LLM judge, write usage rows, or claim a pass/fail score.
    """
    agents: List[str] = []
    seen = set()
    for row in iter_phase(trace, LoopPhase.SPECIALIST):
        agent = row.get("agent")
        if agent and agent not in seen:
            seen.add(agent)
            agents.append(str(agent))
    return {
        "phases": phase_counts(trace),
        "tool_trajectory": tool_trajectory(trace),
        "specialist_agents": agents,
        "eval": "stub",
        "detail": (
            "Trajectory summary only. Not an LLM-as-judge score. "
            "Golden fixtures compare against tool_trajectory; see docs/EVAL.md."
        ),
    }


def assert_known_phases(
    trace: Sequence[Mapping[str, Any]],
    *,
    allow_unknown: bool = False,
) -> Optional[str]:
    """Return an error string if a row uses an unknown phase, else None."""
    known = {p.value for p in LoopPhase}
    for i, row in enumerate(trace):
        label = row.get("phase")
        if label not in known and not allow_unknown:
            return f"trace[{i}] unknown phase: {label!r}"
    return None
