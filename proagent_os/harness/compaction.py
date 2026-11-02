"""Session / conversation context compaction (industry-aligned stub).

Aligns with common agent-platform practice:

* OpenAI Agents / Assistants: manage long sessions by summarizing older turns
  while keeping a recent raw window for the runner
  (https://developers.openai.com/api/docs/guides/agents/running-agents).
* Anthropic long-context guidance: preserve recent tool results; compress
  older history rather than dropping everything.

This module is a **stub**: the default summarizer is extractive
(join + truncate). No live LLM and no tiktoken dependency. Inject a
:class:`Summarizer` for a real model later.

See docs/MEMORY.md and docs/ARCHITECTURE.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, runtime_checkable

# Stored on conversation message rows; converted to system for LLM wire format.
SUMMARY_ROLE = "summary"
SUMMARY_PREFIX = "[Conversation summary]\n"


@runtime_checkable
class Summarizer(Protocol):
    """Pluggable summarizer. Default is dumb join/truncate (no LLM)."""

    def summarize(self, messages: Sequence[Mapping[str, Any]]) -> str:
        """Return a short text covering the given older turns."""


@dataclass
class ExtractiveSummarizer:
    """Heuristic summarizer: role-tagged lines, truncated. Offline / tests."""

    max_chars: int = 2000

    def summarize(self, messages: Sequence[Mapping[str, Any]]) -> str:
        lines: List[str] = []
        for msg in messages:
            role = str(msg.get("role") or "unknown")
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            # Prefer keeping recent-looking tool snippets visible in the fold.
            if role == "tool":
                preview = content if len(content) <= 240 else content[:237] + "..."
                lines.append(f"- tool: {preview}")
            else:
                preview = content if len(content) <= 320 else content[:317] + "..."
                lines.append(f"- {role}: {preview}")
        text = "Earlier turns:\n" + "\n".join(lines) if lines else "Earlier turns: (empty)"
        if len(text) > self.max_chars:
            return text[: self.max_chars - 3] + "..."
        return text


@dataclass(frozen=True)
class CompactionConfig:
    """Thresholds for when and how to compact. Env knobs map here."""

    enabled: bool = True
    # Trigger when non-system message count exceeds this (after filtering).
    max_messages: int = 40
    # Approximate char budget for the returned window (system + summary + recent).
    max_chars: int = 24_000
    # Keep this many most-recent non-system messages raw.
    keep_recent: int = 12
    # Cap for the extractive summary body itself.
    summary_max_chars: int = 2000

    def __post_init__(self) -> None:
        if self.max_messages < 1:
            object.__setattr__(self, "max_messages", 1)
        if self.keep_recent < 1:
            object.__setattr__(self, "keep_recent", 1)
        if self.max_chars < 512:
            object.__setattr__(self, "max_chars", 512)
        if self.summary_max_chars < 64:
            object.__setattr__(self, "summary_max_chars", 64)


@dataclass
class CompactionResult:
    """Outcome of one compaction pass over an in-memory message list."""

    messages: List[Dict[str, Any]]
    compacted: bool = False
    dropped_count: int = 0
    summary_text: str = ""
    reason: str = "noop"
    meta: Dict[str, Any] = field(default_factory=dict)


def approx_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    """Cheap character estimate (token-counting stub; not tiktoken)."""
    total = 0
    for msg in messages:
        total += len(str(msg.get("role") or ""))
        total += len(str(msg.get("content") or ""))
        total += 8  # role separators / framing overhead
    return total


def _is_system_like(msg: Mapping[str, Any]) -> bool:
    role = str(msg.get("role") or "")
    return role in {"system", SUMMARY_ROLE}


def _copy_msg(msg: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(msg)


def _extend_recent_for_tool_pairs(
    non_system: List[Mapping[str, Any]],
    keep_recent: int,
) -> int:
    """Grow the recent window so we do not split assistant tool_calls from tool rows.

    Anthropic-style: prefer preserving recent tool results intact. When the
    cut lands inside a tool batch, pull a few more older messages into the
    raw window (bounded).
    """
    if keep_recent >= len(non_system):
        return len(non_system)
    start = len(non_system) - keep_recent
    # Walk backward while the boundary message is a bare tool result.
    guard = 0
    while start > 0 and guard < 8:
        role = str(non_system[start].get("role") or "")
        if role != "tool":
            break
        start -= 1
        guard += 1
    return len(non_system) - start


def compact_messages(
    messages: Sequence[Mapping[str, Any]],
    config: Optional[CompactionConfig] = None,
    summarizer: Optional[Summarizer] = None,
) -> CompactionResult:
    """Return a compacted view: keep system + last N; fold older into summary.

    Does not mutate the input list. Original rows should stay in storage for
    audit; callers persist a summary row separately when desired.
    """
    cfg = config or CompactionConfig()
    if not cfg.enabled:
        return CompactionResult(
            messages=[_copy_msg(m) for m in messages],
            compacted=False,
            reason="disabled",
        )

    system_msgs = [_copy_msg(m) for m in messages if _is_system_like(m)]
    # Drop prior summary rows from the "older" fold; we rebuild one summary.
    prior_summaries = [m for m in system_msgs if str(m.get("role")) == SUMMARY_ROLE]
    system_only = [m for m in system_msgs if str(m.get("role")) == "system"]
    non_system = [_copy_msg(m) for m in messages if not _is_system_like(m)]

    over_count = len(non_system) > cfg.max_messages
    # Include system in char check for budget pressure.
    over_chars = approx_chars(list(messages)) > cfg.max_chars
    if not over_count and not over_chars:
        return CompactionResult(
            messages=[_copy_msg(m) for m in messages],
            compacted=False,
            reason="under_threshold",
            meta={
                "non_system_count": len(non_system),
                "approx_chars": approx_chars(list(messages)),
            },
        )

    keep_n = _extend_recent_for_tool_pairs(non_system, cfg.keep_recent)
    if keep_n >= len(non_system) and not over_chars:
        # Nothing older to fold; still under meaningful compaction.
        return CompactionResult(
            messages=[_copy_msg(m) for m in messages],
            compacted=False,
            reason="nothing_to_fold",
            meta={"keep_recent": keep_n, "non_system_count": len(non_system)},
        )

    older = non_system[: len(non_system) - keep_n] if keep_n < len(non_system) else []
    recent = non_system[len(non_system) - keep_n :] if keep_n else list(non_system)

    # If only over chars with a small count, still fold the older half.
    if not older and over_chars and len(non_system) > cfg.keep_recent:
        cut = max(1, len(non_system) - cfg.keep_recent)
        older = non_system[:cut]
        recent = non_system[cut:]

    summarizer = summarizer or ExtractiveSummarizer(max_chars=cfg.summary_max_chars)
    # Fold prior summary text into the new summary so we do not lose it.
    fold_input: List[Mapping[str, Any]] = list(prior_summaries) + list(older)
    summary_text = summarizer.summarize(fold_input) if fold_input else ""

    summary_msg: Dict[str, Any] = {
        "role": SUMMARY_ROLE,
        "content": summary_text,
        "meta": {
            "compaction": True,
            "dropped_count": len(older),
            "kept_recent": len(recent),
            "strategy": "keep_system_last_n_fold_older",
            "summarizer": type(summarizer).__name__,
            "approx_chars_before": approx_chars(list(messages)),
        },
    }

    out: List[Dict[str, Any]] = list(system_only) + [summary_msg] + list(recent)

    # Enforce char budget by shrinking recent from the oldest edge (keep newest).
    while len(out) > 1 and approx_chars(out) > cfg.max_chars and len(recent) > 1:
        # Drop the oldest recent message (index after system+summary).
        drop_at = len(system_only) + 1  # first recent
        if drop_at >= len(out):
            break
        removed = out.pop(drop_at)
        recent = recent[1:]
        summary_msg["meta"]["char_budget_dropped"] = summary_msg["meta"].get(
            "char_budget_dropped", 0
        ) + 1
        summary_msg["meta"]["last_char_budget_drop_role"] = removed.get("role")
        # Refresh summary content lightly — keep existing summary; budget is on recent.

    # Truncate summary body if still over budget.
    if approx_chars(out) > cfg.max_chars and summary_text:
        budget_for_summary = max(
            64,
            cfg.max_chars - approx_chars(system_only + recent) - 32,
        )
        if len(summary_text) > budget_for_summary:
            summary_text = summary_text[: budget_for_summary - 3] + "..."
            summary_msg["content"] = summary_text
            out = list(system_only) + [summary_msg] + list(recent)

    return CompactionResult(
        messages=out,
        compacted=True,
        dropped_count=len(older),
        summary_text=summary_text,
        reason="compacted",
        meta=dict(summary_msg["meta"]),
    )


def messages_for_llm(messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize stored roles for chat-completions wire format.

    ``summary`` becomes ``system`` with a fixed prefix. Other roles pass through.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if role == SUMMARY_ROLE:
            text = str(content or "")
            if not text.startswith(SUMMARY_PREFIX.strip()):
                text = SUMMARY_PREFIX + text
            out.append({"role": "system", "content": text})
        else:
            row = {"role": role, "content": content if content is not None else ""}
            # Pass through tool fields when present (specialist in-memory loops).
            if msg.get("tool_call_id"):
                row["tool_call_id"] = msg["tool_call_id"]
            if msg.get("name"):
                row["name"] = msg["name"]
            out.append(row)
    return out


def config_from_settings(settings: Any) -> CompactionConfig:
    """Build :class:`CompactionConfig` from a Settings-like object."""
    return CompactionConfig(
        enabled=bool(getattr(settings, "compaction_enabled", True)),
        max_messages=int(getattr(settings, "compaction_max_messages", 40)),
        max_chars=int(getattr(settings, "compaction_max_chars", 24_000)),
        keep_recent=int(getattr(settings, "compaction_keep_recent", 12)),
        summary_max_chars=int(getattr(settings, "compaction_summary_max_chars", 2000)),
    )
