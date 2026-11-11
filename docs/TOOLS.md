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
4. Calls `ToolRegistry.invoke` **once** when `PROAGENT_HITL_EXECUTE_ON_APPROVE` is true (default). Schema is checked again before the call; a failure does not `run` and does not write a receipt.
5. Before `invoke`, inserts an `hitl_execute_receipts` row (`started`) keyed by a fingerprint of task id, tool name, and args hash. After `invoke`, that row becomes `succeeded` or `failed` and stores an observation snippet. A later resume reuses a finished receipt and does not call `invoke` again. A `started` row while the task is still `awaiting_human` is fail-closed: HTTP 409, no automatic re-run. `{"reexecute": true}` is the explicit re-approve for that in-flight row only and may duplicate the side effect. A `succeeded` or `failed` row is never run again. If the task is already `running` and the receipt for the open checkpoint is finished, the same `POST .../resume` re-injects the stored observation and finishes the plan without a second `invoke`. A `running` task with a `started` receipt is HTTP 409 even with `reexecute`. See [ARCHITECTURE.md](ARCHITECTURE.md).
6. Appends the observation and finishes remaining plan steps.

`POST .../reject` cancels the task and does not call `invoke`. It also cancels a `running` task that still has an open HITL checkpoint, still without calling `invoke`.
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
| `PROAGENT_HITL_EXECUTE_ON_APPROVE` | `true` | On resume, run the paused gated tool once after a second schema check, with an execute receipt so a retry does not run it again. `false` continues without `run`. The receipt guard is always on when this is true; there is no separate switch. See [ARCHITECTURE.md](ARCHITECTURE.md) |

## Stub vs planned

| Stub now | Planned |
| --- | --- |
| Practical JSON Schema subset (stdlib) | Optional full Draft engine / generated **Pydantic** models per tool |
| Global strict flag | Per-tool timeout, approval policy, and rate limits |
| OpenAI function wire format as today | OpenAI **strict** structured-outputs mode (`strict: true` on function tools) end-to-end |
| Observation on failure | Richer repair prompts / automatic re-ask with schema hints |
| One gated `invoke` per fingerprint, with a durable execute receipt | Approval that covers later calls in the same run; multi-process fencing beyond the SQLite unique fingerprint |
| Builtins keep hand-written `parameters` dicts | Codegen from typed tool definitions |

Default `PROAGENT_LLM_PROVIDER=mock` stays offline. Validation never calls a network.

## MCP client (stdio, opt-in)

A thin client for [Model Context Protocol](https://modelcontextprotocol.io) stdio servers. It is **off by default**. Mock demos and CI do not import the adapter and do not start a subprocess. This is not an MCP host, not a marketplace, and not a per-tenant sandbox.

When `PROAGENT_MCP_ENABLED=true`, `build_tool_registry` (used by `AppServices`) parses `PROAGENT_MCP_SERVERS` and, for each usable entry, spawns the process, sends `initialize` plus `notifications/initialized`, and calls `tools/list`. Each advertised tool becomes a normal `Tool` on the existing `ToolRegistry`. The specialist loop is unchanged: the model sees `openai_tools` for names the agent lists, and `ToolRegistry.invoke` validates arguments before `tools/call`.

| Env | Default | Meaning |
| --- | --- | --- |
| `PROAGENT_MCP_ENABLED` | `false` | `true` imports the adapter and starts configured stdio servers. `false` ignores `PROAGENT_MCP_SERVERS` |
| `PROAGENT_MCP_SERVERS` | empty | JSON array of server objects. Ignored when the flag is off. At most 8 entries are used |
| `PROAGENT_MCP_CALL_TIMEOUT_S` | `15` | Per JSON-RPC read timeout, in seconds (max 120). A timeout or a dead process skips that server at startup, or returns an observation on a later call |

Server object (stdio only):

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Short label. The registry name is `mcp_{name}_{tool}` after non-alphanumeric characters become `_` |
| `command` | yes | Executable path. Passed as argv, never through a shell |
| `args` | no | String list. Not split on spaces |
| `env` | no | String keys and values merged onto the child environment. Not logged. Not a secret store |
| `cwd` | no | Working directory for the child |
| `transport` | no | Only `stdio` (the default). `http` or anything else is skipped and does not open a socket |

Example (local process, no network):

```bash
export PROAGENT_MCP_ENABLED=true
export PROAGENT_MCP_SERVERS='[{"name":"notes","command":"python","args":["server.py"]}]'
```

A specialist only sees the tool when its agent `tools:` list includes the registry name (for example `mcp_notes_search`). MCP tools are not added to the always-allowed set (`kb_search`, `clock`, `emit_recommendation`, `request_run_approval`). Names that collide with a tool already on the registry are skipped. Names longer than 64 characters are skipped.

`tools/call` observations are `{text, content}` from MCP text blocks, capped. Non-text blocks are omitted (`omitted: true`). `isError: true` is `ok: false`. Remote tools do not set `requires_human_approval`.

Sessions are process-wide. Every tenant whose agent lists the tool shares that subprocess. The child inherits the API process environment. Shut down stops the processes (`registry.close()` from the API lifespan, the worker, and `reset_services`). There is no reconnect and no `tools/listChanged` refresh.

Wire protocol is newline-delimited JSON-RPC, `initialize` protocol version `2024-11-05`. Server stderr is discarded.

| Shipped | Not in this cut |
| --- | --- |
| Stdio client, `tools/list`, `tools/call`, registry prefix, schema check before the call | Streamable HTTP, SSE, resources, prompts, sampling, roots, OAuth, live tool-list refresh, per-tool approval, per-tenant processes |

`tests/fixtures/mcp_fake_server.py` is a local stub for pytest. It is not a product server.

## Related

- Code: `proagent_os/professional/tools/{base,validation,builtins,mcp_client}.py`
- Architecture map: [ARCHITECTURE.md](ARCHITECTURE.md)
- HITL pause/resume path: [ARCHITECTURE.md](ARCHITECTURE.md#task-run-hitl-pause--resume-mid-loop-continue)
