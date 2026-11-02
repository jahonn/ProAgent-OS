# Tool schemas and argument validation

Specialists call tools through OpenAI-style function schemas (`Tool.openai_tool()` /
`Tool.parameters`). Before a tool runs, ProAgent OS validates the model-supplied
arguments against that JSON schema — the same idea as **OpenAI Agents FunctionTool**
(JSON schema for args, validate before execution) and **Anthropic tool_use**
(reject bad inputs into the observation channel). This is not a new tool DSL;
it hardens the existing registry and builtins.

## What is shipped (stub)

| Piece | Behavior |
| --- | --- |
| Hook | `ToolRegistry.invoke` / `ToolRegistry.validate` in `proagent_os/professional/tools/base.py` |
| Validator | Stdlib subset in `validation.py` (no `jsonschema` dependency) |
| Types | `string`, `number`, `integer`, `boolean`, `object`, `array` (+ `null`); bool is not an integer |
| Required | Missing keys → failure |
| `additionalProperties` | JSON Schema default: **allow** when omitted; reject extras only when `false` |
| Failure | Tool **does not** `run` (no HTTP, no file read, no shell). Specialist gets `{ok:false, output:{error, issues, executed:false}, error}` |
| Trace | Tool phase may set `schema_validation_failed: true` |
| HITL | `requires_human_approval` tools validate **before** pause; bad args become an observation, not `awaiting_human`. On approve, the same snapshot is validated **again** inside `ToolRegistry.invoke` before `run` |
| Knob | `PROAGENT_TOOL_SCHEMA_STRICT` (default `true`) |

## Approve, then execute the gated tool

A tool with `requires_human_approval = True` does not `run` when the model calls
it. The orchestrator stores a snapshot and pauses the task at `awaiting_human`:

| Field | Role |
| --- | --- |
| `kind` | `gated_tool` |
| `tool` | Registered tool name (`^[A-Za-z_][A-Za-z0-9_]{0,63}$`) |
| `tool_call_id` | Model call id |
| `arguments` | Object that already passed schema validation. This is the only payload `invoke` receives |
| `proposed` | Opaque human context (for `request_run_approval`, the `action` object). Not executed |

`POST /api/v1/tasks/{id}/resume` (continue flag on) then:

1. Parses that snapshot. Opaque JSON, URLs, and shell-like names are not calls.
2. Requires the checkpoint `tool_name` and `gated_arguments` to match.
3. Refuses tools that are missing, not allowed for the specialist, or not marked `requires_human_approval` (so a pending payload cannot select `http_fetch`).
4. Calls `ToolRegistry.invoke` **once** when `PROAGENT_HITL_EXECUTE_ON_APPROVE` is true (default). Schema is checked again; a failure does not `run`.
5. Appends the observation and finishes remaining plan steps.

`POST .../reject` cancels the task and does not call `invoke`.
`PROAGENT_HITL_RESUME_CONTINUE=false` is the legacy stub: approve marks
`completed` and does not run the tool. `PROAGENT_HITL_EXECUTE_ON_APPROVE=false`
still continues the loop, with an acknowledgement instead of a tool run.

`request_run_approval.run` records the gate (`side_effect=approval_recorded`)
and echoes `action` as data. It does not perform HTTP or shell. A vertical
tool that sets `requires_human_approval` uses the same pause → approve → one
`invoke` path; its own `run` is the side effect, and only after both checks.

## Settings

| Env | Default | Meaning |
| --- | --- | --- |
| `PROAGENT_TOOL_SCHEMA_STRICT` | `true` | Validate before execute. Set `false` only for deliberate legacy / fixture looseness |
| `PROAGENT_HITL_EXECUTE_ON_APPROVE` | `true` | On resume, run the paused gated tool once after a second schema check. `false` continues without `run`. See [ARCHITECTURE.md](ARCHITECTURE.md) |

## Stub vs planned

| Stub now | Planned |
| --- | --- |
| Practical JSON Schema subset (stdlib) | Optional full Draft engine / generated **Pydantic** models per tool |
| Global strict flag | Per-tool timeout, approval policy, and rate limits |
| OpenAI function wire format as today | OpenAI **strict** structured-outputs mode (`strict: true` on function tools) end-to-end |
| Observation on failure | Richer repair prompts / automatic re-ask with schema hints |
| One gated `invoke` after approve | Crash-safe exactly-once if the process dies after `run` and before `completed`; approval that covers later calls in the same run |
| Builtins keep hand-written `parameters` dicts | Codegen from typed tool definitions |

Default `PROAGENT_LLM_PROVIDER=mock` stays offline. Validation never calls a network.

## Related

- Code: `proagent_os/professional/tools/{base,validation,builtins}.py`
- Architecture map: [ARCHITECTURE.md](ARCHITECTURE.md)
- HITL pause/resume path: [ARCHITECTURE.md](ARCHITECTURE.md#task-run-hitl-pause--resume-mid-loop-continue)
