"""Opt-in MCP stdio client. Not a host, and not used unless enabled.

Speaks newline-delimited JSON-RPC (MCP stdio, protocol ``2024-11-05``) to
local subprocesses. Discovered tools are ordinary ``Tool`` instances on the
existing ``ToolRegistry``. Streamable HTTP, SSE, resources, prompts, sampling,
OAuth, and ``tools/listChanged`` are not implemented.

The child inherits the process environment. ``env`` on a server entry adds or
overrides keys. This is not a sandbox and not a secret store. The command is
an argv list (never a shell). Sessions are process-wide: every tenant that is
allowed to call the tool shares that subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from proagent_os import __version__
from proagent_os.professional.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

logger = logging.getLogger("proagent.mcp")

PROTOCOL_VERSION = "2024-11-05"
_MAX_SERVERS = 8
_MAX_ARGS = 32
_MAX_ENV = 32
_MAX_TEXT = 8000
_MAX_LINE = 1_000_000
_SERVER_PART = 24
_REMOTE_PART = 35
_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class McpClientError(Exception):
    """A stdio session failed. The tool is skipped or the call returns an observation."""


@dataclass
class McpServerSpec:
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None


def registry_tool_name(server: str, remote: str) -> Optional[str]:
    """Map a server + remote tool to ``mcp_{server}_{tool}`` (max 64 chars)."""
    server_part = _UNSAFE.sub("_", server).strip("_")[:_SERVER_PART]
    remote_part = _UNSAFE.sub("_", remote).strip("_")[:_REMOTE_PART]
    if not server_part or not remote_part:
        return None
    name = f"mcp_{server_part}_{remote_part}"
    if len(name) > 64 or _NAME.fullmatch(name) is None:
        return None
    return name


def parse_mcp_server_specs(raw: str) -> List[McpServerSpec]:
    """Parse ``PROAGENT_MCP_SERVERS``. Invalid entries are skipped, not raised."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("PROAGENT_MCP_SERVERS is not valid JSON (%s)", exc.msg)
        return []
    if not isinstance(data, list):
        logger.warning("PROAGENT_MCP_SERVERS must be a JSON array")
        return []
    if len(data) > _MAX_SERVERS:
        logger.warning("PROAGENT_MCP_SERVERS lists %s servers; using the first %s", len(data), _MAX_SERVERS)
        data = data[:_MAX_SERVERS]
    specs: List[McpServerSpec] = []
    for index, item in enumerate(data):
        spec = _parse_one(index, item)
        if spec is not None:
            specs.append(spec)
    return specs


def _parse_one(index: int, item: Any) -> Optional[McpServerSpec]:
    label = f"PROAGENT_MCP_SERVERS[{index}]"
    if not isinstance(item, dict):
        logger.warning("%s is not an object; skipping", label)
        return None
    transport = str(item.get("transport") or "stdio").strip().lower()
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("%s is missing a name; skipping", label)
        return None
    name = name.strip()
    if transport != "stdio":
        logger.warning("MCP server %s transport %r is not implemented; skipping", name, transport)
        return None
    command = item.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        logger.warning("MCP server %s is missing a command; skipping", name)
        return None
    raw_args = item.get("args", [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list) or len(raw_args) > _MAX_ARGS:
        logger.warning("MCP server %s args must be a list of at most %s strings", name, _MAX_ARGS)
        return None
    args: List[str] = []
    for arg in raw_args:
        if not isinstance(arg, str) or "\x00" in arg:
            logger.warning("MCP server %s has a non-string arg; skipping", name)
            return None
        args.append(arg)
    env = _parse_env(name, item.get("env"))
    if env is None:
        return None
    cwd_raw = item.get("cwd")
    cwd: Optional[str] = None
    if cwd_raw is not None:
        if not isinstance(cwd_raw, str) or not cwd_raw.strip() or "\x00" in cwd_raw:
            logger.warning("MCP server %s cwd must be a path string; skipping", name)
            return None
        cwd = cwd_raw
    if registry_tool_name(name, "tool") is None:
        logger.warning("MCP server %s name cannot be used in a tool prefix; skipping", name)
        return None
    return McpServerSpec(name=name, command=command.strip(), args=args, env=env, cwd=cwd)


def _parse_env(name: str, raw: Any) -> Optional[Dict[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > _MAX_ENV:
        logger.warning("MCP server %s env must be an object of at most %s strings", name, _MAX_ENV)
        return None
    env: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or "\x00" in key or not isinstance(value, str) or "\x00" in value:
            logger.warning("MCP server %s env has a non-string entry; skipping the server", name)
            return None
        env[key] = value
    return env


class McpStdioSession:
    """One stdio MCP session. ``call_tool`` is safe to run from a worker thread."""

    def __init__(self, spec: McpServerSpec, *, timeout_s: float) -> None:
        self.spec = spec
        self.timeout_s = timeout_s
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self._closed = False
        self._next_id = 1
        self._stdout_buf = bytearray()
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()

    def start(self) -> None:
        self._spawn()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(5):
            params: Dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = self._request("tools/list", params)
            if not isinstance(result, dict):
                raise McpClientError(f"MCP server {self.spec.name!r} tools/list result was not an object")
            batch = result.get("tools") or []
            if isinstance(batch, list):
                tools.extend(item for item in batch if isinstance(item, dict))
            nxt = result.get("nextCursor")
            if not isinstance(nxt, str) or not nxt:
                break
            cursor = nxt
        return tools

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        with self._state_lock:
            proc = self.proc
            self.proc = None
            self._closed = True
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _spawn(self) -> None:
        env = os.environ.copy()
        env.update(self.spec.env)
        try:
            self.proc = subprocess.Popen(
                [self.spec.command, *self.spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.spec.cwd,
                env=env,
                bufsize=0,
                start_new_session=True,
                shell=False,
            )
        except OSError as exc:
            raise McpClientError(f"failed to start MCP server {self.spec.name!r}: {exc}") from exc

    def _initialize(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "proagent-os", "version": __version__},
            },
        )
        if not isinstance(result, dict):
            raise McpClientError(f"MCP server {self.spec.name!r} initialize result was not an object")
        self._notify("notifications/initialized")

    def _notify(self, method: str) -> None:
        with self._io_lock:
            self._write({"jsonrpc": "2.0", "method": method})

    def _request(self, method: str, params: Optional[Dict[str, Any]]) -> Any:
        with self._io_lock:
            msg_id = self._next_id
            self._next_id += 1
            body: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                body["params"] = params
            self._write(body)
            skipped = 0
            while True:
                msg = self._read_json()
                if not isinstance(msg, dict):
                    raise McpClientError(f"MCP server {self.spec.name!r} sent a non-object message")
                if msg.get("id") != msg_id:
                    skipped += 1
                    if skipped > 50:
                        raise McpClientError(
                            f"MCP server {self.spec.name!r} sent too many unrelated messages"
                        )
                    continue
                if "error" in msg:
                    err = msg.get("error")
                    message = err.get("message") if isinstance(err, dict) else str(err)
                    raise McpClientError(f"{method} failed: {message}")
                if "result" not in msg:
                    raise McpClientError(f"{method} returned no result")
                return msg["result"]

    def _write(self, msg: Dict[str, Any]) -> None:
        with self._state_lock:
            proc = self.proc
            closed = self._closed
        if closed or proc is None or proc.stdin is None:
            raise McpClientError(f"MCP server {self.spec.name!r} is not running")
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpClientError(f"MCP server {self.spec.name!r} write failed: {exc}") from exc

    def _read_json(self) -> Any:
        line = self._read_line()
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpClientError(f"MCP server {self.spec.name!r} sent invalid JSON") from exc

    def _read_line(self) -> str:
        deadline = time.monotonic() + self.timeout_s
        while True:
            nl = self._stdout_buf.find(b"\n")
            if nl != -1:
                raw = bytes(self._stdout_buf[:nl])
                del self._stdout_buf[: nl + 1]
                return raw.decode("utf-8", errors="replace").strip()
            if len(self._stdout_buf) > _MAX_LINE:
                raise McpClientError(f"MCP server {self.spec.name!r} message exceeded 1MB")
            with self._state_lock:
                proc = self.proc
                closed = self._closed
            if closed or proc is None or proc.stdout is None:
                raise McpClientError(f"MCP server {self.spec.name!r} is not running")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpClientError(f"timed out waiting for MCP server {self.spec.name!r}")
            try:
                fd = proc.stdout.fileno()
                readable, _, _ = select.select([fd], [], [], remaining)
                if not readable:
                    raise McpClientError(f"timed out waiting for MCP server {self.spec.name!r}")
                chunk = os.read(fd, 65536)
            except (OSError, ValueError) as exc:
                raise McpClientError(f"MCP server {self.spec.name!r} read failed: {exc}") from exc
            if not chunk:
                code = proc.poll()
                raise McpClientError(f"MCP server {self.spec.name!r} closed stdout (returncode={code})")
            self._stdout_buf.extend(chunk)


class McpProxyTool(Tool):
    """One remote MCP tool, invoked through ``ToolRegistry`` like a builtin."""

    requires_human_approval = False

    def __init__(
        self,
        *,
        registry_name: str,
        description: str,
        parameters: Dict[str, Any],
        session: McpStdioSession,
        remote_name: str,
    ) -> None:
        self.name = registry_name
        self.description = description
        self.parameters = parameters
        self._session = session
        self._remote_name = remote_name

    async def run(self, arguments: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx  # process-wide session; tenant id is not forwarded
        try:
            payload = await asyncio.to_thread(
                self._session.call_tool,
                self._remote_name,
                dict(arguments or {}),
            )
        except McpClientError as exc:
            return ToolResult(ok=False, output=None, error=str(exc))
        return _tool_result(payload)


def _tool_result(payload: Any) -> ToolResult:
    if not isinstance(payload, dict):
        return ToolResult(ok=False, output=None, error="MCP tools/call result was not an object")
    is_error = bool(payload.get("isError"))
    content = payload.get("content")
    blocks = content if isinstance(content, list) else []
    safe_blocks: List[Dict[str, Any]] = []
    texts: List[str] = []
    for block in blocks[:8]:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            piece = str(block.get("text") or "")
            if len(piece) > _MAX_TEXT:
                piece = piece[: _MAX_TEXT - 3] + "..."
            safe_blocks.append({"type": "text", "text": piece})
            texts.append(piece)
        else:
            safe_blocks.append({"type": str(block.get("type") or "unknown"), "omitted": True})
    text = "\n".join(texts)
    if len(text) > _MAX_TEXT:
        text = text[: _MAX_TEXT - 3] + "..."
    output: Dict[str, Any] = {"text": text, "content": safe_blocks}
    if "structuredContent" in payload:
        structured = payload.get("structuredContent")
        try:
            raw = json.dumps(structured, ensure_ascii=False, default=str)
        except TypeError:
            raw = ""
        if raw and len(raw) <= _MAX_TEXT:
            output["structured"] = structured
    if is_error:
        return ToolResult(ok=False, output=output, error=text or "MCP tool error")
    return ToolResult(ok=True, output=output, error=None)


def _parameters(schema: Any) -> Dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    if schema.get("type") not in (None, "object"):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    out = dict(schema)
    out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    return out


def _description(server: str, remote: str, raw: Any) -> str:
    if isinstance(raw, str) and raw.strip():
        text = " ".join(raw.split())
    else:
        text = f"MCP tool {remote} from server {server}"
    if len(text) > 1024:
        text = text[:1021] + "..."
    return f"[mcp:{server}] {text}"


def attach_mcp_servers(registry: ToolRegistry, settings: Any) -> int:
    """Start configured stdio servers and register their tools.

    Failures are logged and that server is skipped. Returns how many tools
    were registered. ``registry.close()`` stops the subprocesses.
    """
    if not getattr(settings, "mcp_enabled", False):
        return 0
    specs = parse_mcp_server_specs(getattr(settings, "mcp_servers", "") or "")
    if not specs:
        logger.info("PROAGENT_MCP_ENABLED is set and no stdio servers were registered")
        return 0
    timeout_s = float(getattr(settings, "mcp_call_timeout_s", 15.0))
    open_sessions: List[McpStdioSession] = []
    added = 0
    try:
        for spec in specs:
            session = McpStdioSession(spec, timeout_s=timeout_s)
            try:
                session.start()
                remote_tools = session.list_tools()
            except McpClientError as exc:
                logger.warning("MCP server %s unavailable: %s", spec.name, exc)
                session.close()
                continue
            count = _register_remote_tools(registry, session, remote_tools)
            if count == 0:
                session.close()
                continue
            open_sessions.append(session)
            added += count
            logger.info("MCP server %s registered %s tool(s)", spec.name, count)
    except Exception:
        for session in open_sessions:
            session.close()
        raise
    if open_sessions:
        registry.add_closer(lambda sessions=list(open_sessions): _close_sessions(sessions))
    return added


def _register_remote_tools(
    registry: ToolRegistry,
    session: McpStdioSession,
    remote_tools: List[Dict[str, Any]],
) -> int:
    added = 0
    seen: set[str] = set()
    for item in remote_tools:
        remote = item.get("name")
        if not isinstance(remote, str) or not remote.strip():
            continue
        remote = remote.strip()
        reg_name = registry_tool_name(session.spec.name, remote)
        if reg_name is None:
            logger.warning("MCP tool %s/%s skipped; name is not usable", session.spec.name, remote)
            continue
        if reg_name in seen or registry.get(reg_name) is not None:
            logger.warning("MCP tool %s skipped; name already registered", reg_name)
            continue
        seen.add(reg_name)
        tool = McpProxyTool(
            registry_name=reg_name,
            description=_description(session.spec.name, remote, item.get("description")),
            parameters=_parameters(item.get("inputSchema")),
            session=session,
            remote_name=remote,
        )
        registry.register(tool)
        added += 1
    return added


def _close_sessions(sessions: List[McpStdioSession]) -> None:
    for session in sessions:
        try:
            session.close()
        except Exception:
            logger.exception("failed to close MCP server %s", session.spec.name)
