#!/usr/bin/env python3
"""Newline-delimited JSON-RPC MCP stdio stub for tests.

Not a product server. CI starts it as a local subprocess. It does not open a
socket and does not read API keys. Tools: ``ping``, ``echo``, ``fail``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

PROTOCOL = "2024-11-05"
CALLS = 0

TOOLS = [
    {
        "name": "ping",
        "description": "Return a pong observation. Optional note is echoed.",
        "inputSchema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "echo",
        "description": "Echo text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fail",
        "description": "Return an MCP tool error.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def send(msg: Dict[str, Any]) -> None:
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def result(msg_id: Any, payload: Dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "result": payload})


def error(msg_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def ping_text(arguments: Dict[str, Any]) -> str:
    mark = os.environ.get("PROAGENT_MCP_FAKE_MARK", "")
    text = f"pong:{CALLS}"
    if mark:
        text += f":{mark}"
    note = arguments.get("note")
    if isinstance(note, str) and note:
        text += f":{note}"
    return text


def call_tool(arguments: Any, name: str) -> Dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    if name == "ping":
        return {"content": [{"type": "text", "text": ping_text(args)}], "isError": False}
    if name == "echo":
        return {
            "content": [{"type": "text", "text": str(args.get("text") or "")}],
            "isError": False,
        }
    if name == "fail":
        return {"content": [{"type": "text", "text": "boom"}], "isError": True}
    raise KeyError(name)


def handle(msg: Dict[str, Any]) -> None:
    global CALLS
    method = msg.get("method")
    if "id" not in msg:
        return
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        version = params.get("protocolVersion")
        result(
            msg_id,
            {
                "protocolVersion": version if isinstance(version, str) and version else PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "proagent-mcp-fake", "version": "0.0.1"},
            },
        )
        return
    if method == "tools/list":
        result(msg_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        CALLS += 1
        if not isinstance(name, str):
            error(msg_id, -32602, "tool name required")
            return
        try:
            result(msg_id, call_tool(params.get("arguments"), name))
        except KeyError:
            error(msg_id, -32602, f"unknown tool: {name}")
        return
    error(msg_id, -32601, f"method not found: {method}")


def main() -> int:
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return 0
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            error(None, -32700, "parse error")
            continue
        if isinstance(msg, dict):
            handle(msg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        raise SystemExit(0)
