from aiohttp import web


def add_routes(app):
    app.router.add_get("/health", health)
    app.router.add_get("/provisioning/pubkey", provisioning_pubkey)


async def health(request):
    # Advertise the WS gateway port so the phone can derive its WebSocket URL.
    # The HTTP control API and the WS gateway run on separate ports; without
    # this the phone reuses the HTTP port for the WS upgrade and fails with
    # "Expected HTTP 101 response". Omitted when no gateway is configured
    # (gateway_port is None) so bare-test callers keep the {"status":"ok"} shape.
    gateway_port = request.app.get("sense_gateway_port")
    body = {"status": "ok"}
    if gateway_port is not None:
        body["gatewayPort"] = gateway_port
    return web.json_response(body)


async def provisioning_pubkey(request):
    get_pubkey = request.app["sense_get_pubkey"]
    pk = get_pubkey()
    if pk is None:
        return web.json_response({"error": "no_key"}, status=503)
    return web.json_response({"pubkey": pk.hex(), "key_id": "default", "created_at": None})