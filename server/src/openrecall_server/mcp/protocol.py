"""A hand-rolled MCP JSON-RPC subset: initialize, tools/list, tools/call, ping.

Deliberately dependency-free. The official `mcp` SDK does install on Python
3.14, but it pulls a second ASGI stack (starlette + uvicorn) into a process
that is already aiohttp. The subset we need is small enough to own.

Tool handlers raise for their own failures; a raising handler becomes an MCP
*tool error* (`isError: true` inside a successful JSON-RPC result), not a
transport error, because the agent needs to see and recover from it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "sense"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._tools[spec.name] = spec

    def list(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)


def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code,
                                                      "message": message}}


async def dispatch(registry: ToolRegistry,
                   request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request. Returns None for a notification."""
    req_id = request.get("id")
    method = request.get("method")
    is_notification = "id" not in request

    if not isinstance(method, str):
        return None if is_notification else _err(req_id, INVALID_REQUEST,
                                                 "missing method")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [
            {"name": t.name, "description": t.description,
             "inputSchema": t.input_schema}
            for t in registry.list()
        ]}
    elif method == "tools/call":
        params = request.get("params") or {}
        spec = registry.get(params.get("name"))
        if spec is None:
            return None if is_notification else _err(
                req_id, INVALID_PARAMS, f"unknown tool: {params.get('name')}")
        args = params.get("arguments") or {}
        try:
            payload = await spec.handler(args)
        except Exception as exc:                     # tool error, not transport
            log.warning("mcp_tool_failed tool=%s", spec.name, exc_info=True)
            result = {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            }
        else:
            result = {
                "isError": False,
                "structuredContent": payload,
                "content": [{"type": "text",
                             "text": json.dumps(payload, default=str)}],
            }
    else:
        return None if is_notification else _err(req_id, METHOD_NOT_FOUND,
                                                 f"unknown method: {method}")

    return None if is_notification else _ok(req_id, result)
