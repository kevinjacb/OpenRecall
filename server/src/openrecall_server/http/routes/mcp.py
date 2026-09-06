"""POST /mcp — the MCP endpoint (spec §5.2).

In-process on the existing aiohttp app rather than a stdio server, so tools
call the same stores the 35 HTTP routes use. A stdio server would have the
client spawn a second Sense process against the same SQLite files.
"""
from __future__ import annotations

import json

from aiohttp import web

from ...mcp.protocol import PARSE_ERROR, dispatch


def add_routes(app: web.Application) -> None:
    app.router.add_post("/mcp", post_mcp)


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
    if isinstance(body, list):                        # JSON-RPC batch
        out = [r for r in [await dispatch(registry, m) for m in body]
               if r is not None]
        return web.json_response(out)
    result = await dispatch(registry, body)
    if result is None:
        return web.Response(status=202)
    return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))
