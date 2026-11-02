"""Load golden fixtures and compare tool trajectories.

Matching mode for this stub is **ordered_subset**: every name in
``expected_tools`` must appear in ``actual`` in the same order, with extra
tools allowed between and around them. Exact equality is not required.

This mirrors the commercial intent of LangSmith complex-agent trajectory
checks and Google ADK tool-trajectory criteria, without an LLM-as-judge or
live LangSmith/ADK wiring.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from proagent_os.harness.loop import summarize_run, tool_trajectory

# Default directory used by pytest when no path is passed.
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval"

MatchMode = str  # documented: only "ordered_subset" in this stub


@dataclass(frozen=True)
class GoldenTask:
    """One offline golden task loaded from JSON."""

    id: str
    task: str
    expected_tools: List[str]
    seed_kb: List[Dict[str, str]] = field(default_factory=list)
    match: MatchMode = "ordered_subset"
    description: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str = "") -> "GoldenTask":
        tid = str(data.get("id") or "").strip()
        if not tid:
            raise ValueError(f"golden fixture missing id{': ' + source if source else ''}")
        task = str(data.get("task") or "").strip()
        if not task:
            raise ValueError(f"golden fixture {tid!r} missing task")
        expected = data.get("expected_tools")
        if not isinstance(expected, list) or not all(isinstance(x, str) for x in expected):
            raise ValueError(f"golden fixture {tid!r} expected_tools must be a list of strings")
        match = str(data.get("match") or "ordered_subset")
        if match != "ordered_subset":
            raise ValueError(
                f"golden fixture {tid!r} unsupported match={match!r}; "
                "this stub only supports ordered_subset"
            )
        seeds_raw = data.get("seed_kb") or []
        if not isinstance(seeds_raw, list):
            raise ValueError(f"golden fixture {tid!r} seed_kb must be a list")
        seeds: List[Dict[str, str]] = []
        for i, item in enumerate(seeds_raw):
            if not isinstance(item, Mapping):
                raise ValueError(f"golden fixture {tid!r} seed_kb[{i}] must be an object")
            title = str(item.get("title") or "").strip() or "seed"
            text = str(item.get("text") or "").strip()
            if not text:
                raise ValueError(f"golden fixture {tid!r} seed_kb[{i}] missing text")
            seeds.append({"title": title, "text": text})
        return cls(
            id=tid,
            task=task,
            expected_tools=[str(x) for x in expected],
            seed_kb=seeds,
            match=match,
            description=str(data.get("description") or ""),
        )


@dataclass
class EvalResult:
    """Structured pass/fail for one golden comparison (no LLM score)."""

    task_id: str
    passed: bool
    match: MatchMode
    expected_tools: List[str]
    actual_tools: List[str]
    diffs: List[str] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    metering: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compare_trajectory(
    actual: Sequence[str],
    expected: Sequence[str],
    *,
    match: MatchMode = "ordered_subset",
    task_id: str = "adhoc",
) -> EvalResult:
    """Compare tool name sequences. Returns structured pass/fail + diffs."""
    actual_list = [str(x) for x in actual]
    expected_list = [str(x) for x in expected]
    if match != "ordered_subset":
        return EvalResult(
            task_id=task_id,
            passed=False,
            match=match,
            expected_tools=expected_list,
            actual_tools=actual_list,
            diffs=[f"unsupported match mode: {match!r} (stub supports ordered_subset only)"],
        )
    diffs = _ordered_subset_diffs(actual_list, expected_list)
    return EvalResult(
        task_id=task_id,
        passed=not diffs,
        match=match,
        expected_tools=expected_list,
        actual_tools=actual_list,
        diffs=diffs,
    )


def _ordered_subset_diffs(actual: Sequence[str], expected: Sequence[str]) -> List[str]:
    """Require expected as an ordered subsequence of actual."""
    diffs: List[str] = []
    if not expected:
        return diffs
    i = 0
    for want in expected:
        found = False
        while i < len(actual):
            if actual[i] == want:
                found = True
                i += 1
                break
            i += 1
        if not found:
            diffs.append(
                f"missing expected tool {want!r} in order "
                f"(expected={list(expected)!r}, actual={list(actual)!r})"
            )
            # Continue reporting remaining missing names without advancing.
            # Reset scan so later expected names can still be checked loosely.
            # After a miss, keep searching from the end position.
    return diffs


def load_golden_tasks(
    fixtures_dir: Union[str, Path, None] = None,
) -> List[GoldenTask]:
    """Load ``*.json`` golden fixtures from a directory (sorted by id)."""
    root = Path(fixtures_dir) if fixtures_dir is not None else DEFAULT_FIXTURES_DIR
    if not root.is_dir():
        raise FileNotFoundError(f"golden fixtures directory not found: {root}")
    tasks: List[GoldenTask] = []
    for path in sorted(root.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, Mapping):
            raise ValueError(f"{path}: root must be a JSON object")
        tasks.append(GoldenTask.from_mapping(data, source=str(path)))
    return tasks


def maybe_record_eval_metering(
    tenant_id: Optional[str],
    *,
    task_id: str,
    passed: bool,
) -> Optional[Dict[str, Any]]:
    """Optional metering hook.

    ``USAGE_KINDS`` does not yet include an eval kind, so this stub returns a
    descriptive dict and does **not** call ``record_usage``. Wiring a real
    ``eval_run`` kind later is intentional follow-up work.
    """
    if not tenant_id:
        return None
    return {
        "recorded": False,
        "reason": "eval_run kind not in USAGE_KINDS; metering stub only",
        "tenant_id": tenant_id,
        "task_id": task_id,
        "passed": passed,
        "billing": "stub",
    }


async def run_golden_task(
    golden: GoldenTask,
    *,
    tenant_id: Optional[str] = None,
    record_metering: bool = True,
) -> EvalResult:
    """Run one golden task through the Orchestrator under the current LLM provider.

    Callers should set ``PROAGENT_LLM_PROVIDER=mock`` for deterministic CI.
    """
    from proagent_os.db import get_db, new_id, utcnow
    from proagent_os.services import get_services

    tid = tenant_id or new_id("ten")
    db = get_db()
    if tenant_id is None:
        db.insert(
            "tenants",
            {
                "id": tid,
                "name": f"Eval {golden.id}",
                "api_key": f"pk_eval_{golden.id}",
                "goals": "Golden-task evaluation",
                "constraints": "",
                "tone": "professional",
                "created_at": utcnow(),
            },
        )

    svc = get_services()
    for seed in golden.seed_kb:
        svc.kb.ingest_text(tid, "manual", seed["title"], seed["text"])

    result = await svc.orchestrator.run_task(tid, golden.task)
    trace = result.get("trace") or []
    actual = tool_trajectory(trace)
    eval_result = compare_trajectory(
        actual,
        golden.expected_tools,
        match=golden.match,
        task_id=golden.id,
    )
    eval_result.summary = {
        **summarize_run(trace),
        "orchestrator_status": result.get("status"),
        "task_row_id": result.get("task_id"),
    }
    if record_metering:
        eval_result.metering = maybe_record_eval_metering(
            tid, task_id=golden.id, passed=eval_result.passed
        )
    return eval_result
