"""ONE MCP stdio server: JSON-RPC 2.0, newline-delimited MCP tool calls.

Uses MCP legacy initialize/tools interface for broad installed CLI compatibility.
No process execution, code changes, secrets, approvals or arbitrary reads via MCP.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from . import __version__
from .store import Store

PROTOCOLS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
DEFAULT_PROTOCOL = "2025-06-18"


def _field(typ="string", description=""):
    return {"type": typ, "description": description}


def _spec(name: str, description: str, properties: dict, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False}}


TOOLS = [
    _spec("one_create_task", "Queue a development task; does NOT execute a model or approve spending.", {
        "title": _field(description="Short task title"),
        "goal": _field(description="Specific goal, max 8000 characters; no secrets"),
        "assignee": _field(description="grok-cli|claude|codex|antigravity|operator|grok-bot"),
        "owner": _field(description="The coordinator label (not authenticated)"),
        "base_commit": _field(description="Exact git commit SHA for an isolated worktree"),
        "allowed_paths": {"type": "array", "items": {"type": "string"}},
        "acceptance": {"type": "array", "items": {"type": "string"}},
        "budget": _field("integer", "Advisory provider usage budget (not a verified quota)"),
        "request_key": _field(description="Idempotency key, reuse to avoid duplicate dispatches"),
    }, ("title", "goal", "assignee")),
    _spec("one_list_tasks", "List bounded task metadata.", {
        "assignee": _field(description="Filter assigned agent"),
        "status": _field(description="Filter by queued/running/blocked/completed/failed/cancelled"),
        "limit": _field("integer", "Maximum 100, default 25"),
    }),
    _spec("one_get_task", "Retrieve one task by ID.", {"task_id": _field()}, ("task_id",)),
    _spec("one_send_message", "Send a redacted, bounded task-related message to another agent inbox.", {
        "task_id": _field(), "sender": _field(), "recipient": _field(), "body": _field()
    }, ("task_id", "sender", "recipient", "body")),
    _spec("one_inbox", "Retrieve bounded agent inbox; use one_ack_message after reading.", {
        "recipient": _field(), "limit": _field("integer"), "unread_only": _field("boolean")
    }, ("recipient",)),
    _spec("one_ack_message", "Mark a message read for its designated recipient.", {
        "message_id": _field(), "recipient": _field()
    }, ("message_id", "recipient")),
    _spec("one_register_artifact", "Register artifact metadata ONLY; the bytes are not read or verified by MCP.", {
        "task_id": _field(), "producer": _field(), "kind": _field(), "path": _field(), "digest": _field()
    }, ("task_id", "producer", "kind", "path", "digest")),
    _spec("one_list_artifacts", "List artifact path+hash references for task; unverified client claims.", {
        "task_id": _field()
    }, ("task_id",)),
    _spec("one_task_events", "Read bounded task event history without full logs.", {
        "task_id": _field(), "limit": _field("integer")
    }, ("task_id",)),
]
TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}


def validate_args(tool_name: str, args: Any):
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    schema = TOOL_INDEX[tool_name]["inputSchema"]
    if set(args) - schema["properties"].keys():
        raise ValueError("unexpected tool argument")
    if set(schema["required"]) - args.keys():
        raise ValueError("required tool argument missing")
    for key, value in args.items():
        expected = schema["properties"][key]["type"]
        valid = (expected == "string" and isinstance(value, str)) or (
            expected == "integer" and type(value) is int) or (
            expected == "boolean" and type(value) is bool) or (
            expected == "array" and isinstance(value, list))
        if not valid:
            raise ValueError(f"incorrect type for {key}: expected {expected}")
        if expected == "array" and not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must contain strings")


def call_tool(store: Store, name: str, args: dict):
    actions = {
        "one_create_task": lambda: store.create_task(**args),
        "one_list_tasks": lambda: store.list_tasks(**args),
        "one_get_task": lambda: store.task(**args),
        "one_send_message": lambda: store.message(**args),
        "one_inbox": lambda: store.inbox(**args),
        "one_ack_message": lambda: store.ack(**args),
        "one_register_artifact": lambda: store.add_artifact(**args),
        "one_list_artifacts": lambda: store.artifacts(**args),
        "one_task_events": lambda: store.events(**args),
    }
    return actions[name]()


def dispatch(store: Store, method: str, params: dict):
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {"protocolVersion": requested if requested in PROTOCOLS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "one-mcp", "version": __version__},
                "instructions": "ONE manages software-development tasks, not security targets. "
                                "Tasks are NOT executed or approved through MCP. "
                                "Do not submit secrets. Operator approval is required for CLI execution."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_INDEX:
            raise ValueError("unknown tool")
        args = params.get("arguments") or {}
        validate_args(name, args)
        return {"content": [{"type": "text", "text": json.dumps(call_tool(store, name, args),
                                                               ensure_ascii=False, separators=(",", ":"))}],
                "isError": False}
    raise KeyError(method)


def handle(store: Store, request: Any):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in request:
        return None  # MCP notification, including notifications/initialized
    rid = request["id"]
    method = request["method"]
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid params"}}
    try:
        return {"jsonrpc": "2.0", "id": rid, "result": dispatch(store, method, params)}
    except KeyError:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
    except (ValueError, TypeError) as exc:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": str(exc)[:300]}], "isError": True}}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(exc)[:300]}}
    except Exception:
        logging.exception("ONE MCP operation failed")
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32603, "message": "Internal error"}}


def serve(input_stream=None, output_stream=None, store=None):
    incoming = input_stream or sys.stdin
    outgoing = output_stream or sys.stdout
    owned = store is None
    store = store or Store()
    try:
        for line in incoming:
            if not line.strip():
                continue
            if len(line) > 65536:
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Request exceeds 64 KiB"}}
            else:
                try:
                    response = handle(store, json.loads(line))
                except json.JSONDecodeError:
                    response = {"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": "Parse error"}}
            if response is not None:
                outgoing.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                outgoing.flush()
    finally:
        if owned:
            store.close()


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    serve()


if __name__ == "__main__":
    main()
