# Evaluation — golden tasks and tool trajectories

ProAgent OS scores golden tasks with a **deterministic trajectory matcher**.
An optional LLM judge can run after that check. Nothing here uploads traces to
LangSmith or Google ADK.

Scores stay in the local `EvalResult` report. The runner loads JSON fixtures,
runs the Orchestrator (usually under `PROAGENT_LLM_PROVIDER=mock`), extracts
`tool_trajectory(trace)` from `proagent_os/harness/loop.py`, and compares that
list to the fixture’s `expected_tools`.

## Trajectory check (always)

**`ordered_subset`.** Every name in `expected_tools` must appear in the actual
tool sequence **in the same order**. Extra tools before, between, or after are
allowed. Exact full-sequence equality is **not** required (exact / ADK-style
strict scores are planned).

That comparison always runs. Its pass/fail is `trajectory_passed`. Human-readable
misses stay in `diffs`. The fixture field `match` is this trajectory rule and
still accepts only `ordered_subset`.

## Optional LLM judge

`PROAGENT_EVAL_JUDGE` chooses what sets the reported `passed` value:

| Value | Behavior |
| --- | --- |
| `ordered_subset` (default) | No chat call. `passed` is the trajectory result. `judge_mode=ordered_subset`. CI uses this. |
| `llm` | After the trajectory check, one chat completion scores the case. A parsed verdict sets `passed` and `judge_mode=llm`. |

The judge is `LlmJudge` in `proagent_os/eval/judge.py`. It calls the existing
chat client (`get_llm()`): mock in CI, or the OpenAI-compatible client when
`PROAGENT_LLM_PROVIDER=openai` and an API key is set. A missing key follows the
factory and uses the mock provider. The call sends no tools.

The rubric is a short system prompt plus a JSON user message:

- `task`
- `expected_tools`
- `actual_tools`
- `final_answer` (orchestrator aggregate text)

The model must reply with JSON only:

```json
{"passed": true, "rationale": "one or two sentences"}
```

`passed` has to be a JSON boolean and `rationale` a non-empty string. Markdown
fences around that object are accepted. `judge_rationale` on the report is the
rationale, clipped. Trajectory `diffs` are kept either way, so the judge can
disagree with the matcher (for example a matching tool path and an empty
final answer).

Inject a `Judge` into `run_golden_task(..., judge=...)` to bypass the flag in
tests.

### Mock provider

`PROAGENT_LLM_PROVIDER=mock` does not call the network. When the system prompt
is the judge rubric, the mock returns deterministic JSON:

- **pass** when expected tools are an ordered subset of the actual tools and
  `final_answer` is non-empty
- **fail** otherwise (missing or out-of-order tools, or an empty answer)

The rationales are fixed strings (`MOCK_JUDGE_PASS_RATIONALE` /
`MOCK_JUDGE_FAIL_RATIONALE`). The mock treats a non-empty final answer as
satisfying the answer half of the rubric. A live model is asked to check that
the prose addresses the task.

### Fallback

If the chat call raises, or the completion is empty or not the JSON object
above, the runner **keeps the ordered_subset result**:

- `passed` is `trajectory_passed`
- `judge_mode` is `ordered_subset_fallback`
- `judge_fallback` is the exception type plus a short message
- `judge_rationale` is empty

A trajectory miss stays a miss when the judge is down. The runner makes one call.

`EvalResult.to_dict()` is the eval report (`judge_mode`, `judge_rationale`,
`judge_fallback`, `trajectory_passed`, `final_answer`, plus the trajectory
fields).

## Layout

| Path | Role |
| --- | --- |
| `proagent_os/eval/golden.py` | Load fixtures, `compare_trajectory`, `run_golden_task` |
| `proagent_os/eval/judge.py` | Rubric, JSON parse, `LlmJudge`, fallback |
| `tests/fixtures/eval/*.json` | Golden tasks (`id`, `task`, `expected_tools`, optional `seed_kb`) |
| `tests/test_eval_golden.py` | Matcher tests, mock Orchestrator runs, judge pass/fail/fallback |
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

`match` is the trajectory rule, not the judge flag. Set the judge with
`PROAGENT_EVAL_JUDGE`.

## Metering

When a golden eval finishes, `maybe_record_eval_metering` appends one
`usage_events` row with `kind=eval_run`. **Quantity is the number of cases
evaluated in that call**, not a flat 1 for a multi-case batch and not a
pass/fail score.

`run_golden_task` (the helper the mock CI tests call) evaluates **one** case.
It passes `cases=1`, so the row’s `quantity` is 1. Pass and fail both count:
the case was evaluated. A second finish appends another row. `GET /api/v1/usage`
sums `quantity` per tenant and kind, so the `eval_run` total is the number of
cases evaluated for that tenant. `events` is the number of finished calls.

A caller that finishes several cases in one call passes `cases` set to that
count. The single row then stores the case count (for example `cases=3` stores
`quantity=3`). Do not also leave `record_metering` on for each inner
`run_golden_task`, or those cases are counted twice.

`record_metering=False` skips the insert. No `tenant_id` inserts nothing and
the hook returns `None`. The row does not store the task id or the score. The
`metering` field on `EvalResult` echoes them: `{recorded: true, kind: "eval_run",
quantity, billing: "stub", task_id, passed, …}`. `passed` there is the reported
score (judge verdict when `judge_mode=llm`, otherwise the trajectory result).

Tenant scoping matches the other usage kinds. A tenant bearer sees only its
rows. The admin key sees every tenant, or one tenant when `tenant_id` is set.
The payload stays `billing: stub`. There is no payment provider and no charge.

## Stub vs planned

| Shipped now | Planned (not in this cut) |
| --- | --- |
| JSON golden fixtures + ordered-subset matcher | Exact match / ADK criterion scores |
| Mock Orchestrator pytest path | Live LangSmith datasets / complex-agent eval |
| Optional LLM judge (`PROAGENT_EVAL_JUDGE=llm`) with ordered-subset fallback | Multi-criterion rubrics, numeric scores, hosted eval |
| Structured `EvalResult` report | Hosted eval dashboards |
| `eval_run` usage kind (quantity = cases evaluated; `billing: stub`) | Session compaction of long eval traces |

## Industry references

- LangSmith — [Evaluate a complex agent](https://docs.langchain.com/langsmith/evaluate-complex-agent) (trajectory / tool-path checks; hosted stacks also score with an LLM judge)
- Google ADK — [Evaluation criteria](https://adk.dev/evaluate/criteria/) (tool-trajectory and related scores)
- Google ADK overview — [Agent Development Kit](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/adk)

Default `PROAGENT_EVAL_JUDGE=ordered_subset` and `PROAGENT_LLM_PROVIDER=mock`
stay offline and green in CI (3.11 / 3.12). `llm` plus the mock provider is
deterministic and still offline.
