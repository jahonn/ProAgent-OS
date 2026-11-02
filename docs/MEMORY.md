# Conversation memory and session compaction

ProAgent OS stores chat turns in SQLite (`conversations` / `messages`) via
`ConversationMemory`. Long sessions need a **compaction** path so model calls
do not grow without bound — the same idea as OpenAI Agents session management
and Anthropic’s guidance to keep recent tool results while compressing older
history.

## What is shipped (stub)

| Piece | Behavior |
| --- | --- |
| Thresholds | `PROAGENT_COMPACTION_*` settings (see below) |
| Strategy | Keep `system` messages + last **N** non-system turns raw; fold older into one `summary` role message |
| Summarizer | `ExtractiveSummarizer` (join + truncate). Inject a `Summarizer` protocol for a real LLM later |
| Token estimate | `approx_chars` character budget — **not** tiktoken |
| Audit | Originals stay in `messages`. Compaction appends a `summary` row with meta (`compaction`, `dropped_count`, …) and optional `tenant_memory` kind=`compaction` |
| Orchestrator | After each user turn, `compact_if_needed`; planner/specialist get a short prior-context block; task trace may include phase `compaction` |

Wire helpers: `compact_messages`, `messages_for_llm`, `ConversationMemory.history_for_llm`.

## Settings

| Env | Default | Meaning |
| --- | --- | --- |
| `PROAGENT_COMPACTION_ENABLED` | `true` | Master switch |
| `PROAGENT_COMPACTION_MAX_MESSAGES` | `40` | Trigger when non-system count exceeds this |
| `PROAGENT_COMPACTION_MAX_CHARS` | `24000` | Approx char budget for the compacted window |
| `PROAGENT_COMPACTION_KEEP_RECENT` | `12` | Raw recent turns to keep (tool pairs prefer not to split) |
| `PROAGENT_COMPACTION_SUMMARY_MAX_CHARS` | `2000` | Cap for the extractive summary body |

## Stub vs planned

| Stub now | Planned |
| --- | --- |
| Extractive join/truncate summarizer | LLM summarizer (inject `Summarizer`; optional dedicated model) |
| Character budget (`approx_chars`) | Optional tiktoken / provider token counts (extra, not required) |
| Global env thresholds | Per-tenant budgets and retention policies |
| Soft view + appended summary row | Hard rewrite / archival of compacted tails |
| Prior-context text block into planner/specialist | Full multi-turn session object for the runner (OpenAI-style) |
| Trace phase `compaction` | Dashboard of compaction events and cost savings |

Default `PROAGENT_LLM_PROVIDER=mock` stays offline. Compaction never calls a
live model in CI.

## Related

- Code: `proagent_os/harness/compaction.py`, `proagent_os/harness/memory.py`
- Loop phase: `LoopPhase.COMPACTION` in `proagent_os/harness/loop.py`
- Architecture map: [ARCHITECTURE.md](ARCHITECTURE.md)
