"""Optional LLM-as-judge for golden-task evaluation.

``PROAGENT_EVAL_JUDGE`` defaults to ``ordered_subset``: the trajectory matcher
in ``golden.py`` is the whole score and this module is not called.

``llm`` runs after that matcher. One chat completion (no tools) sees the task,
expected tools, actual tools, and final answer, and must reply with JSON
``{"passed": bool, "rationale": "..."}``. The mock provider returns a
deterministic verdict. A raised call or unparseable completion does not fail
the run by itself: the caller keeps the ordered-subset result and records
``judge_mode=ordered_subset_fallback``.

Inject a :class:`Judge` in tests. The default provider is
:func:`proagent_os.llm.factory.get_llm` (mock in CI; OpenAI-compatible when
``PROAGENT_LLM_PROVIDER=openai`` and a key is set).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

# Leading sentence is the mock-provider marker (see llm/mock.py). Keep it stable.
JUDGE_SYSTEM = (
    "You are an evaluation judge for a ProAgent golden task. "
    "Pass only if every expected tool appears in the actual tool list in the same "
    "order (extra tools are allowed) and the final answer addresses the task. "
    "Fail if a tool is missing, out of order, or the final answer is empty or off-task. "
    'Reply with JSON only: {"passed": true or false, "rationale": "one or two sentences"}. '
    "Do not call tools. Do not add a preamble."
)

# Prompt caps. The report may still store the full final answer.
_TASK_CHARS = 2000
_ANSWER_CHARS = 4000
_TOOL_CHARS = 200
_MAX_TOOLS = 64
_RATIONALE_CHARS = 500


class JudgeParseError(ValueError):
    """Model text was not the required ``passed`` + ``rationale`` object."""


@dataclass(frozen=True)
class JudgeVerdict:
    """Structured judge score. ``passed`` is the judge's own boolean."""

    passed: bool
    rationale: str


@dataclass(frozen=True)
class JudgeApplication:
    """Outcome of one judge attempt, including a fallback.

    ``mode`` is ``llm`` when the verdict parsed, otherwise
    ``ordered_subset_fallback``. ``passed`` is set only for ``llm``.
    """

    mode: str
    passed: Optional[bool]
    rationale: str
    fallback: str


@runtime_checkable
class Judge(Protocol):
    """Pluggable judge. :class:`LlmJudge` is the chat-provider implementation."""

    async def judge(
        self,
        *,
        task: str,
        expected_tools: Sequence[str],
        actual_tools: Sequence[str],
        final_answer: str,
    ) -> JudgeVerdict:
        """Return a structured pass/fail. Raise on transport or parse failure."""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _tool_list(names: Sequence[str]) -> list[str]:
    clipped: list[str] = []
    for name in list(names)[:_MAX_TOOLS]:
        clipped.append(_clip(str(name), _TOOL_CHARS))
    return clipped


def judge_user_payload(
    *,
    task: str,
    expected_tools: Sequence[str],
    actual_tools: Sequence[str],
    final_answer: str,
) -> str:
    """JSON object the judge model (and the mock parser) reads. No preamble."""
    payload = {
        "task": _clip(task.strip(), _TASK_CHARS),
        "expected_tools": _tool_list(expected_tools),
        "actual_tools": _tool_list(actual_tools),
        "final_answer": _clip(final_answer.strip(), _ANSWER_CHARS),
    }
    return json.dumps(payload, ensure_ascii=False)


def _load_json_object(text: str) -> Any:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(raw[start : end + 1])


def parse_judge_output(text: str) -> JudgeVerdict:
    """Parse a judge completion. Raises :class:`JudgeParseError` when it is not usable."""
    if not isinstance(text, str) or not text.strip():
        raise JudgeParseError("empty judge output")
    try:
        data = _load_json_object(text)
    except json.JSONDecodeError as exc:
        snippet = " ".join(text.split())[:120]
        raise JudgeParseError(f"unparseable judge output: {snippet}") from exc
    if not isinstance(data, dict):
        raise JudgeParseError("unparseable judge output: JSON value is not an object")
    passed = data.get("passed")
    if not isinstance(passed, bool):
        raise JudgeParseError("unparseable judge output: passed must be a JSON boolean")
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise JudgeParseError("unparseable judge output: rationale must be a non-empty string")
    return JudgeVerdict(passed=passed, rationale=_clip(rationale.strip(), _RATIONALE_CHARS))


def _fallback_reason(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = " ".join(text.split())
    if len(text) > 240:
        return text[:237] + "..."
    return text


class LlmJudge:
    """One OpenAI-compatible chat call (or the mock) with the golden-task rubric.

    Pass ``provider`` in tests. Otherwise :func:`get_llm` supplies the
    configured client. Tools are not offered. Parse failures raise
    :class:`JudgeParseError` so the caller can fall back to ordered_subset.
    """

    def __init__(self, *, provider: Any = None) -> None:
        self._provider = provider

    async def judge(
        self,
        *,
        task: str,
        expected_tools: Sequence[str],
        actual_tools: Sequence[str],
        final_answer: str,
    ) -> JudgeVerdict:
        from proagent_os.llm.base import LLMMessage
        from proagent_os.llm.factory import get_llm

        provider = self._provider if self._provider is not None else get_llm()
        response = await provider.chat(
            [
                LLMMessage(role="system", content=JUDGE_SYSTEM),
                LLMMessage(
                    role="user",
                    content=judge_user_payload(
                        task=task,
                        expected_tools=expected_tools,
                        actual_tools=actual_tools,
                        final_answer=final_answer,
                    ),
                ),
            ],
            temperature=0.0,
            max_tokens=256,
            tools=None,
            tool_choice=None,
        )
        content = getattr(response, "content", None)
        return parse_judge_output("" if content is None else str(content))


def judge_for_settings(settings: Any) -> Optional[LlmJudge]:
    """Return an :class:`LlmJudge` when the flag is ``llm``; otherwise ``None``.

    ``None`` keeps :func:`proagent_os.eval.golden.run_golden_task` on the
    ordered-subset score with no chat call.
    """
    mode = str(getattr(settings, "eval_judge", "ordered_subset") or "ordered_subset")
    mode = mode.strip().lower()
    if mode == "llm":
        return LlmJudge()
    return None


async def run_judge(
    judge: Judge,
    *,
    task: str,
    expected_tools: Sequence[str],
    actual_tools: Sequence[str],
    final_answer: str,
) -> JudgeApplication:
    """Call ``judge`` and convert failures into an ordered-subset fallback."""
    try:
        verdict = await judge.judge(
            task=task,
            expected_tools=expected_tools,
            actual_tools=actual_tools,
            final_answer=final_answer,
        )
    except Exception as exc:
        reason = _fallback_reason(exc)
        logger.warning("golden-task llm judge failed; using ordered_subset (%s)", reason)
        return JudgeApplication(
            mode="ordered_subset_fallback",
            passed=None,
            rationale="",
            fallback=reason,
        )
    if not isinstance(verdict, JudgeVerdict):
        reason = "unparseable judge output: judge did not return JudgeVerdict"
        logger.warning("golden-task llm judge failed; using ordered_subset (%s)", reason)
        return JudgeApplication(
            mode="ordered_subset_fallback",
            passed=None,
            rationale="",
            fallback=reason,
        )
    return JudgeApplication(
        mode="llm",
        passed=verdict.passed,
        rationale=verdict.rationale,
        fallback="",
    )
