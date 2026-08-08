from aiohttp import web

from openrecall_server.http.token import constant_time_eq


@web.middleware
async def bearer_auth_middleware(request, handler):
    token = request.app["sense_token"]
    if token is None:
        request["authorized"] = True
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and constant_time_eq(auth[7:], token):
        request["authorized"] = True
        return await handler(request)
    return web.json_response({"error": "unauthorized"}, status=401)