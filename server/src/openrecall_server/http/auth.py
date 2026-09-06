from aiohttp import web

from openrecall_server.mcp.principal import (
    PRINCIPAL_RELAY, may_reach, principal_for,
)


@web.middleware
async def bearer_auth_middleware(request, handler):
    token = request.app["sense_token"]
    hermes_token = request.app.get("sense_hermes_token")
    if token is None:
        # No auth configured (the shape most tests build). Treat the caller
        # as the relay principal so route-class gating still applies.
        request["authorized"] = True
        request["sense_principal"] = PRINCIPAL_RELAY
        if not may_reach(PRINCIPAL_RELAY, request.path):
            return web.json_response(
                {"error": "forbidden_for_principal"}, status=403)
        return await handler(request)
    principal = principal_for(request, relay_token=token,
                              hermes_token=hermes_token)
    if principal is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not may_reach(principal, request.path):
        return web.json_response({"error": "forbidden_for_principal"},
                                 status=403)
    request["authorized"] = True
    request["sense_principal"] = principal
    return await handler(request)
