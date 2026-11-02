"""Load golden fixtures and compare tool trajectories.

The trajectory matcher is **ordered_subset**: every name in ``expected_tools``
must appear in ``actual`` in the same order, with extra tools allowed between
and around them. Exact equality is not required.

That check always runs and is the default score (``PROAGENT_EVAL_JUDGE=ordered_subset``).
``llm`` calls :mod:`proagent_os.eval.judge` afterwards. A parsed verdict replaces
``passed``; a failed or unparseable call keeps this matcher and sets
``judge_mode=ordered_subset_fallback``.

This mirrors LangSmith trajectory checks and Google ADK tool-trajectory
criteria, plus an optional local LLM judge. It does not upload traces to
LangSmith or ADK.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from proagent_os.harness.loop import summarize_run, tool_trajectory
from proagent_os.metering import BILLING_STUB, record_usage

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
    """Structured pass/fail for one golden comparison.

    ``passed`` is the ordered-subset result unless ``judge_mode`` is ``llm``,
    in which case it is the judge verdict. ``trajectory_passed`` and ``diffs``
    always describe the matcher. ``to_dict`` is the eval report.
    """

    task_id: str
    passed: bool
    match: MatchMode
    expected_tools: List[str]
    actual_tools: List[str]
    diffs: List[str] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    metering: Optional[Dict[str, Any]] = None
    # ordered_subset (default, no chat call) | llm | ordered_subset_fallback
    judge_mode: str = "ordered_subset"
    judge_rationale: str = ""
    judge_fallback: str = ""
    trajectory_passed: Optional[bool] = None
    final_answer: str = ""

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
            judge_mode="ordered_subset",
            trajectory_passed=False,
        )
    diffs = _ordered_subset_diffs(actual_list, expected_list)
    passed = not diffs
    return EvalResult(
        task_id=task_id,
        passed=passed,
        match=match,
        expected_tools=expected_list,
        actual_tools=actual_list,
        diffs=diffs,
        judge_mode="ordered_subset",
        trajectory_passed=passed,
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
    cases: int = 1,
) -> Optional[Dict[str, Any]]:
    """Append one ``eval_run`` usage row when a golden eval finishes.

    ``quantity`` is the number of cases evaluated in this call (``cases``).
    :func:`run_golden_task` evaluates one case and passes ``cases=1``. A caller
    that finishes several cases in one call passes that count so the row stores
    the case count, not a flat 1 for the whole batch. Pass and fail both count.
    The usage aggregate sums ``quantity``, so the tenant total is the number of
    cases. No ``tenant_id`` inserts nothing. This is the billing stub: no charge.
    """
    if not tenant_id:
        return None
    amount = int(cases)
    if amount < 1:
        raise ValueError("cases must be >= 1")
    row = record_usage(tenant_id, "eval_run", amount)
    return {
        "recorded": True,
        "kind": "eval_run",
        "quantity": amount,
        "event_id": row["id"],
        "tenant_id": tenant_id,
        "task_id": task_id,
        "passed": bool(passed),
        "billing": BILLING_STUB,
    }


def _select_judge(judge: Any) -> Any:
    """Explicit judge wins. Otherwise follow ``PROAGENT_EVAL_JUDGE``."""
    if judge is not None:
        return judge
    from proagent_os.config import get_settings
    from proagent_os.eval.judge import judge_for_settings

    return judge_for_settings(get_settings())


async def _attach_judge(
    eval_result: EvalResult,
    *,
    task: str,
    final_answer: str,
    judge: Any,
) -> EvalResult:
    """Run the judge after the trajectory check and fold the outcome into the report."""
    from proagent_os.eval.judge import run_judge

    if eval_result.trajectory_passed is None:
        eval_result.trajectory_passed = eval_result.passed
    application = await run_judge(
        judge,
        task=task,
        expected_tools=eval_result.expected_tools,
        actual_tools=eval_result.actual_tools,
        final_answer=final_answer,
    )
    eval_result.final_answer = final_answer
    if application.mode == "llm" and application.passed is not None:
        eval_result.passed = application.passed
        eval_result.judge_mode = "llm"
        eval_result.judge_rationale = application.rationale
        eval_result.judge_fallback = ""
        return eval_result
    eval_result.passed = bool(eval_result.trajectory_passed)
    eval_result.judge_mode = "ordered_subset_fallback"
    eval_result.judge_rationale = ""
    eval_result.judge_fallback = application.fallback or "unparseable judge output"
    return eval_result


async def run_golden_task(
    golden: GoldenTask,
    *,
    tenant_id: Optional[str] = None,
    record_metering: bool = True,
    judge: Any = None,
) -> EvalResult:
    """Run one golden task through the Orchestrator under the current LLM provider.

    Callers should set ``PROAGENT_LLM_PROVIDER=mock`` for deterministic CI.
    The trajectory check always runs. ``judge`` overrides ``PROAGENT_EVAL_JUDGE``
    when passed (including tests). ``None`` follows the setting, which defaults
    to ordered_subset and does not call a model.

    When ``record_metering`` is true (the default), a finished case appends one
    ``usage_events`` row with ``kind=eval_run`` and ``quantity=1`` (this helper
    evaluates one case). Pass and fail both count. ``record_metering=False``
    skips the insert. See :func:`maybe_record_eval_metering`.
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
    final_answer = result.get("result")
    eval_result.final_answer = final_answer if isinstance(final_answer, str) else str(final_answer or "")
    eval_result.summary = {
        **summarize_run(trace),
        "orchestrator_status": result.get("status"),
        "task_row_id": result.get("task_id"),
    }
    selected = _select_judge(judge)
    if selected is not None:
        eval_result = await _attach_judge(
            eval_result,
            task=golden.task,
            final_answer=eval_result.final_answer,
            judge=selected,
        )
    if record_metering:
        eval_result.metering = maybe_record_eval_metering(
            tid, task_id=golden.id, passed=eval_result.passed, cases=1
        )
    return eval_result
