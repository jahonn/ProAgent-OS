"""Golden-task evaluation.

Default score is ordered-subset trajectory matching. ``PROAGENT_EVAL_JUDGE=llm``
adds an optional chat judge after that check. See ``docs/EVAL.md``.
"""

from proagent_os.eval.golden import (
    EvalResult,
    GoldenTask,
    compare_trajectory,
    load_golden_tasks,
    run_golden_task,
)
from proagent_os.eval.judge import JudgeVerdict, LlmJudge

__all__ = [
    "EvalResult",
    "GoldenTask",
    "JudgeVerdict",
    "LlmJudge",
    "compare_trajectory",
    "load_golden_tasks",
    "run_golden_task",
]
