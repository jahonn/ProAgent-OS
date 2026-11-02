"""Golden-task / tool-trajectory evaluation stub.

Compares ``tool_trajectory(trace)`` to fixture expectations. No LLM judge.
See ``docs/EVAL.md``.
"""

from proagent_os.eval.golden import (
    EvalResult,
    GoldenTask,
    compare_trajectory,
    load_golden_tasks,
    run_golden_task,
)

__all__ = [
    "EvalResult",
    "GoldenTask",
    "compare_trajectory",
    "load_golden_tasks",
    "run_golden_task",
]
