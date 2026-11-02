from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from proagent_os.llm.base import LLMMessage, LLMProvider, LLMResponse, ToolCall, ToolChoice


def _agent_kind(role_hint: str) -> str:
    """Identify specialist from system prompt identity, not skill body text."""
    h = role_hint or ""
    first = h.split("\n", 1)[0]
    if "you are the researcher" in h or "researcher specialist" in h:
        return "researcher"
    if "you are the analyst" in h or "analyst specialist" in h:
        return "analyst"
    if "market scout" in h:
        return "researcher"
    if "you are the planner" in h:
        return "planner"
    if "you are the aggregator" in h:
        return "aggregator"
    if "researcher" in first:
        return "researcher"
    if "analyst" in first:
        return "analyst"
    return "specialist"


def _tc(name: str, arguments: Dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=f"call_{uuid4().hex[:10]}",
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def _tool_names(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    if not tools:
        return []
    names: List[str] = []
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))
        elif isinstance(t, dict) and t.get("name"):
            names.append(str(t["name"]))
    return names


def _extract_query(text: str) -> str:
    m = re.search(r"(?:task|query|question|goal)\s*[:=]\s*(.+)", text, re.I)
    if m:
        return m.group(1).strip()[:200]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "overview"


# Stable text for PROAGENT_COMPACTION_SUMMARIZER=llm under the mock provider.
# Distinct from ExtractiveSummarizer output ("Earlier turns:").
MOCK_COMPACTION_SUMMARY = "Mock compaction summary of earlier turns."
_COMPACTION_SUMMARY_MARKER = "you summarize older conversation turns"

# Stable rationales for PROAGENT_EVAL_JUDGE=llm under the mock provider.
# Pass requires expected tools as an ordered subset and a non-empty final answer.
# The system-prompt marker must stay aligned with eval/judge.py JUDGE_SYSTEM.
MOCK_JUDGE_PASS_RATIONALE = (
    "Mock judge: expected tools appear in order and the final answer is present."
)
MOCK_JUDGE_FAIL_RATIONALE = (
    "Mock judge: expected tools are missing or out of order, or the final answer is empty."
)
_JUDGE_SYSTEM_MARKER = "you are an evaluation judge"

# Console "Run mock approval" includes this token. Other mock prompts ignore it.
# The planner then asks for one analyst step, and that specialist calls
# request_run_approval before emit_recommendation. Not a harness change.
MOCK_HITL_DEMO_MARKER = "proagent-hitl-demo"


def _is_compaction_summary_request(messages: List[LLMMessage]) -> bool:
    """True when the call is the session-compaction summarizer, not a planner."""
    for message in messages:
        if message.role == "system" and _COMPACTION_SUMMARY_MARKER in (
            message.content or ""
        ).lower():
            return True
    return False


def _is_eval_judge_request(messages: List[LLMMessage]) -> bool:
    """True when the call is the golden-task judge, not a planner or summarizer."""
    for message in messages:
        if message.role == "system" and _JUDGE_SYSTEM_MARKER in (message.content or "").lower():
            return True
    return False


def _tools_in_order(actual: List[str], expected: List[str]) -> bool:
    """Same ordered-subset rule as the golden matcher, local so mock does not import eval."""
    index = 0
    for want in expected:
        found = False
        while index < len(actual):
            if actual[index] == want:
                found = True
                index += 1
                break
            index += 1
        if not found:
            return False
    return True


def _mock_judge_passed(user_text: str) -> bool:
    try:
        data = json.loads(user_text)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    expected = data.get("expected_tools")
    actual = data.get("actual_tools")
    answer = data.get("final_answer")
    if not isinstance(expected, list) or not isinstance(actual, list):
        return False
    if not all(isinstance(name, str) for name in expected):
        return False
    if not all(isinstance(name, str) for name in actual):
        return False
    if not isinstance(answer, str) or not answer.strip():
        return False
    return _tools_in_order(actual, expected)


def _mock_judge_content(messages: List[LLMMessage]) -> str:
    user_text = ""
    for message in messages:
        if message.role == "user" and message.content:
            user_text = message.content
    passed = _mock_judge_passed(user_text)
    rationale = MOCK_JUDGE_PASS_RATIONALE if passed else MOCK_JUDGE_FAIL_RATIONALE
    return json.dumps({"passed": passed, "rationale": rationale}, ensure_ascii=False)


class MockLLMProvider(LLMProvider):
    """Deterministic offline LLM stub with native multi-step tool_calls."""

    async def chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: ToolChoice = None,
    ) -> LLMResponse:
        # Before planner/tool heuristics: a long thread can contain those phrases.
        # Judge and compaction calls must stay short deterministic completions.
        if _is_eval_judge_request(messages):
            return LLMResponse(content=_mock_judge_content(messages), model="mock-eval-judge")
        if _is_compaction_summary_request(messages):
            return LLMResponse(content=MOCK_COMPACTION_SUMMARY, model="mock-compaction")

        joined = "\n".join((m.content or "") for m in messages)
        role_hint = ""
        for m in messages:
            if m.role == "system":
                role_hint = (m.content or "").lower()
                break

        tool_names = _tool_names(tools)
        prior_tool_msgs = [m for m in messages if m.role == "tool"]
        prior_assistant_tool_rounds = sum(1 for m in messages if m.role == "assistant" and m.tool_calls)

        if not tools and ("planner" in role_hint or "create a short plan" in joined.lower()):
            if MOCK_HITL_DEMO_MARKER in joined:
                content = json.dumps(
                    {
                        "steps": [
                            {
                                "agent": "analyst",
                                "goal": "Pause for human approval before continuing",
                                "tools": ["request_run_approval"],
                            }
                        ],
                        "summary": "Ask a human to approve this run.",
                    }
                )
                return LLMResponse(content=content, model="mock-planner")
            content = json.dumps(
                {
                    "steps": [
                        {
                            "agent": "researcher",
                            "goal": "Gather relevant knowledge and facts",
                            "tools": ["kb_search", "http_fetch", "clock"],
                        },
                        {
                            "agent": "analyst",
                            "goal": "Analyze findings and draft recommendation",
                            "tools": ["kb_search", "emit_recommendation", "clock"],
                        },
                    ],
                    "summary": "Research then analyze and recommend.",
                }
            )
            return LLMResponse(content=content, model="mock-planner")

        if not tools and ("aggregator" in role_hint or "synthesize" in joined.lower()):
            return LLMResponse(
                content=(
                    "## Summary\n"
                    "Based on specialist findings, here is a concise answer.\n\n"
                    "Key points were retrieved from the knowledge base and tools. "
                    "A recommendation has been proposed where appropriate.\n\n"
                    "_Generated by MockLLM for offline demo._"
                ),
                model="mock-aggregator",
            )

        if tools:
            return self._specialist_with_tools(
                messages=messages,
                role_hint=role_hint,
                joined=joined,
                tool_names=tool_names,
                prior_tool_msgs=prior_tool_msgs,
                prior_assistant_tool_rounds=prior_assistant_tool_rounds,
                tool_choice=tool_choice,
            )

        # Legacy JSON-in-content path
        kind = _agent_kind(role_hint)
        if kind == "analyst":
            content = json.dumps(
                {
                    "thought": "Drafting an actionable recommendation from findings.",
                    "tool_calls": [
                        {
                            "name": "emit_recommendation",
                            "arguments": {
                                "title": "Review and act on latest findings",
                                "rationale": "Mock analyst suggests a proactive follow-up based on KB/context.",
                                "action": {"type": "review", "priority": "medium"},
                            },
                        }
                    ],
                    "answer": "",
                }
            )
            return LLMResponse(content=content, model="mock-analyst-legacy")
        if kind == "researcher" or "tool" in role_hint:
            query = _extract_query(joined)
            content = json.dumps(
                {
                    "thought": "Searching knowledge base for relevant context.",
                    "tool_calls": [{"name": "kb_search", "arguments": {"query": query, "top_k": 3}}],
                    "answer": "",
                }
            )
            return LLMResponse(content=content, model="mock-researcher-legacy")

        return LLMResponse(
            content=(
                "I reviewed the available context and tool results. "
                "Here is a practical response suitable for a professional agent demo. "
                f"Input length={len(joined)} chars."
            ),
            model="mock-default",
        )

    def _specialist_with_tools(
        self,
        *,
        messages: List[LLMMessage],
        role_hint: str,
        joined: str,
        tool_names: List[str],
        prior_tool_msgs: List[LLMMessage],
        prior_assistant_tool_rounds: int,
        tool_choice: ToolChoice,
    ) -> LLMResponse:
        if tool_choice == "none":
            return LLMResponse(
                content="Final answer without further tools (tool_choice=none).",
                model="mock-forced-none",
            )

        kind = _agent_kind(role_hint)

        if kind == "researcher":
            if prior_assistant_tool_rounds == 0 and "kb_search" in tool_names:
                query = _extract_query(joined)
                return LLMResponse(
                    content=None,
                    model="mock-researcher",
                    tool_calls=[_tc("kb_search", {"query": query, "top_k": 3})],
                )
            if prior_assistant_tool_rounds == 1 and "clock" in tool_names:
                return LLMResponse(
                    content=None,
                    model="mock-researcher",
                    tool_calls=[_tc("clock", {})],
                )
            return LLMResponse(
                content=(
                    "Research complete. KB and clock tools were used. "
                    "Findings support a conversion/activation follow-up."
                ),
                model="mock-researcher",
            )

        if kind == "analyst":
            if (
                prior_assistant_tool_rounds == 0
                and "request_run_approval" in tool_names
                and MOCK_HITL_DEMO_MARKER in joined
            ):
                return LLMResponse(
                    content=None,
                    model="mock-analyst",
                    tool_calls=[
                        _tc(
                            "request_run_approval",
                            {
                                "reason": "Mock demo: confirm before continuing this run",
                                "action": {"kind": "demo_gate"},
                            },
                        )
                    ],
                )
            if prior_assistant_tool_rounds == 0 and "emit_recommendation" in tool_names:
                return LLMResponse(
                    content=None,
                    model="mock-analyst",
                    tool_calls=[
                        _tc(
                            "emit_recommendation",
                            {
                                "title": "Review and act on latest findings",
                                "rationale": "Mock analyst suggests a proactive follow-up based on KB/context.",
                                "action": {"type": "review", "priority": "medium"},
                            },
                        )
                    ],
                )
            if prior_assistant_tool_rounds == 0 and "kb_search" in tool_names:
                return LLMResponse(
                    content=None,
                    model="mock-analyst",
                    tool_calls=[_tc("kb_search", {"query": _extract_query(joined), "top_k": 3})],
                )
            return LLMResponse(
                content="Recommendation drafted for human approval. Analysis complete.",
                model="mock-analyst",
            )

        if prior_assistant_tool_rounds == 0 and tool_names:
            name = tool_names[0]
            args: Dict[str, Any] = {}
            if name == "kb_search":
                args = {"query": _extract_query(joined), "top_k": 3}
            return LLMResponse(content=None, model="mock-specialist", tool_calls=[_tc(name, args)])
        return LLMResponse(content="Specialist finished after tool use.", model="mock-specialist")
