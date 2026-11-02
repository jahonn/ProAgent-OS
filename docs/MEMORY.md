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
| Summarizer | `ExtractiveSummarizer` (join + truncate) by default. `PROAGENT_COMPACTION_SUMMARIZER=llm` calls the configured chat provider; failure falls back to extractive and records `summarizer_fallback` |
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
| `PROAGENT_COMPACTION_SUMMARY_MAX_CHARS` | `2000` | Cap for the summary body (extractive or LLM) |
| `PROAGENT_COMPACTION_SUMMARIZER` | `extractive` | `extractive` (no model call) or `llm` (configured chat provider) |

## Summarizer modes

| Mode | Behavior |
| --- | --- |
| `extractive` (default) | `ExtractiveSummarizer`: role-tagged lines, truncated. No chat call. |
| `llm` | `LlmSummarizer` sends older turns to `get_llm()` with a short system prompt and no tools. `PROAGENT_LLM_PROVIDER=mock` returns `Mock compaction summary of earlier turns.` `openai` with an API key uses the OpenAI-compatible chat client. A missing key follows the existing factory and uses the mock provider. |
| LLM failure | Empty or raised completions fall back to extractive. Summary meta and the `compaction` trace gain `summarizer_mode=extractive_fallback` and `summarizer_fallback` (exception class and a short message). A successful `llm` pass sets `summarizer_mode=llm`. |

Keep-last-N and the tool-pair boundary are applied before the summarizer runs, in both modes. Inject a `Summarizer` on `ConversationMemory` to bypass the flag in tests.

The orchestrator persists a summary (`compact_if_needed`) and then builds the prior-context block with a second compaction pass. In `llm` mode that is a second chat call. Both passes share the keep-last-N rules and the same fallback.

## Stub vs planned

| Stub now | Planned |
| --- | --- |
| Extractive default; optional LLM summarizer behind `PROAGENT_COMPACTION_SUMMARIZER` | Dedicated compaction model, separate from the planner |
| Character budget (`approx_chars`) | Optional tiktoken / provider token counts (extra, not required) |
| Global env thresholds | Per-tenant budgets and retention policies |
| Soft view + appended summary row | Hard rewrite / archival of compacted tails |
| Prior-context text block into planner/specialist | Full multi-turn session object for the runner (OpenAI-style) |
| Trace phase `compaction` (plus fallback fields when the LLM path is used) | Dashboard of compaction events and cost savings |

Default `PROAGENT_LLM_PROVIDER=mock` and `PROAGENT_COMPACTION_SUMMARIZER=extractive` stay offline. CI does not call a live model. `llm` plus the mock provider is deterministic and still offline.

## Related

- Code: `proagent_os/harness/compaction.py`, `proagent_os/harness/memory.py`
- Loop phase: `LoopPhase.COMPACTION` in `proagent_os/harness/loop.py`
- Architecture map: [ARCHITECTURE.md](ARCHITECTURE.md)
