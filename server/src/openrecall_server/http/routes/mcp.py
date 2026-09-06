"""POST /mcp — the MCP endpoint (spec §5.2).

In-process on the existing aiohttp app rather than a stdio server, so tools
call the same stores the 35 HTTP routes use. A stdio server would have the
client spawn a second Sense process against the same SQLite files.
"""
from __future__ import annotations

import json

from aiohttp import web

from ...mcp.protocol import INVALID_REQUEST, PARSE_ERROR, dispatch


def add_routes(app: web.Application) -> None:
    app.router.add_post("/mcp", post_mcp)


def _invalid_request(message: str) -> dict:
    return {"jsonrpc": "2.0", "id": None,
            "error": {"code": INVALID_REQUEST, "message": message}}


async def post_mcp(request: web.Request) -> web.Response:
    registry = request.app.get("sense_mcp_registry")
    if registry is None:
        return web.json_response(
            {"error": {"code": "mcp_not_configured"}}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": PARSE_ERROR,
                                            "message": "invalid JSON"}})
    # `dispatch` reads `request.get("id")`, so anything that is not a dict (a
    # bare `5`, a string, a null, or a list member that is not an object)
    # would raise AttributeError and surface as a 500. JSON-RPC calls that
    # Invalid Request, and a well-formed error is far more useful to a client
    # than a stack trace.
    if isinstance(body, list):                        # JSON-RPC batch
        if not body:
            # An empty batch is Invalid Request per JSON-RPC 2.0 §6, not an
            # empty result array.
            return web.json_response(_invalid_request("empty batch"))
        out = [
            r for r in [
                await dispatch(registry, m) if isinstance(m, dict)
                else _invalid_request("request must be an object")
                for m in body
            ]
            if r is not None
        ]
        return web.json_response(out)
    if not isinstance(body, dict):
        return web.json_response(_invalid_request("request must be an object"))
    result = await dispatch(registry, body)
    if result is None:
        return web.Response(status=202)
    return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))
