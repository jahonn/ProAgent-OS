# Evaluation — golden tasks and tool trajectories

ProAgent OS ships a **deterministic trajectory stub**, not an LLM-as-judge or a
hosted LangSmith/ADK runner. The stub loads JSON fixtures, runs the Orchestrator
(usually under `PROAGENT_LLM_PROVIDER=mock`), extracts
`tool_trajectory(trace)` from `proagent_os/harness/loop.py`, and compares that
list to the fixture’s `expected_tools`.

## Matching mode (this cut)

**`ordered_subset` only.** Every name in `expected_tools` must appear in the
actual tool sequence **in the same order**. Extra tools before, between, or
after are allowed. Exact full-sequence equality is **not** required in this
stub (exact / ADK-style strict scores are planned).

Pass/fail comes back as `EvalResult` with `diffs` (human-readable miss reasons).
There is no numeric LLM score.

## Layout

| Path | Role |
| --- | --- |
| `proagent_os/eval/golden.py` | Load fixtures, `compare_trajectory`, `run_golden_task` |
| `tests/fixtures/eval/*.json` | Golden tasks (`id`, `task`, `expected_tools`, optional `seed_kb`) |
| `tests/test_eval_golden.py` | Unit matcher tests + mock Orchestrator golden runs |
| `docs/ARCHITECTURE.md` | Harness loop + industry map |

Example fixture fields:

```json
{
  "id": "research_kb_clock",
  "task": "Why is conversion low and what should we do?",
  "match": "ordered_subset",
  "expected_tools": ["kb_search", "clock"],
  "seed_kb": [{"title": "Product", "text": "…"}]
}
```

## Metering

`maybe_record_eval_metering` is a **no-write stub**. `USAGE_KINDS` does not yet
include `eval_run`, so nothing is inserted into `usage_events`. The hook returns
`{recorded: false, billing: stub, …}` for forward wiring.

## Stub vs planned

| Shipped now | Planned (not in this cut) |
| --- | --- |
| JSON golden fixtures + ordered-subset matcher | Exact match / ADK criterion scores |
| Mock Orchestrator pytest path | Live LangSmith datasets / complex-agent eval |
| Structured `EvalResult` diffs | LLM-as-judge final-answer rubrics |
| Metering hook (no DB write) | `eval_run` usage kind + dashboards |
| Docs linking LangSmith + ADK | LLM-as-judge + live LangSmith datasets |

## Industry references

- LangSmith — [Evaluate a complex agent](https://docs.langchain.com/langsmith/evaluate-complex-agent) (trajectory / tool-path checks)
- Google ADK — [Evaluation criteria](https://adk.dev/evaluate/criteria/) (tool-trajectory and related scores)
- Google ADK overview — [Agent Development Kit](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/adk)

Default `PROAGENT_LLM_PROVIDER=mock` stays offline and green in CI (3.11 / 3.12).
