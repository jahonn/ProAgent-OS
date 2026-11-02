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
| HITL | `requires_human_approval` tools validate **before** pause; bad args become an observation, not `awaiting_human` |
| Knob | `PROAGENT_TOOL_SCHEMA_STRICT` (default `true`) |

## Settings

| Env | Default | Meaning |
| --- | --- | --- |
| `PROAGENT_TOOL_SCHEMA_STRICT` | `true` | Validate before execute. Set `false` only for deliberate legacy / fixture looseness |

## Stub vs planned

| Stub now | Planned |
| --- | --- |
| Practical JSON Schema subset (stdlib) | Optional full Draft engine / generated **Pydantic** models per tool |
| Global strict flag | Per-tool timeout, approval policy, and rate limits |
| OpenAI function wire format as today | OpenAI **strict** structured-outputs mode (`strict: true` on function tools) end-to-end |
| Observation on failure | Richer repair prompts / automatic re-ask with schema hints |
| Builtins keep hand-written `parameters` dicts | Codegen from typed tool definitions |

Default `PROAGENT_LLM_PROVIDER=mock` stays offline. Validation never calls a network.

## Related

- Code: `proagent_os/professional/tools/{base,validation,builtins}.py`
- Architecture map: [ARCHITECTURE.md](ARCHITECTURE.md)
- HITL pause/resume path: [ARCHITECTURE.md](ARCHITECTURE.md#task-run-hitl-pause--resume-mid-loop-continue)
