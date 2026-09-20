from aiohttp import web


def add_routes(app):
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/provisioning/pubkey", provisioning_pubkey)


async def root(request):
    """Diagnose the commonest remote-access misconfiguration.

    The HTTP control API and the WebSocket gateway are two listeners on two
    ports. Put one hostname in front of only the HTTP port and the relay's
    WebSocket lands here, where there is no upgrade handler — and aiohttp
    answers 404, which the client reports as "Expected HTTP 101 response".
    Neither mentions the actual cause, so people go looking at the WebSocket
    code instead of at their proxy.

    Through a *correctly* configured tunnel this handler is unreachable: the
    root path is routed to the gateway. It only answers when the routing is
    wrong, which is exactly when the explanation is needed.
    """
    if request.headers.get("Upgrade", "").lower() == "websocket":
        gateway_port = request.app.get("sense_gateway_port")
        return web.json_response(
            {
                "error": "websocket_sent_to_http_api",
                "detail": (
                    "This is the HTTP control API; it has no WebSocket "
                    "endpoint. The gateway is a separate listener"
                    + (f" on port {gateway_port}." if gateway_port else ".")
                    + " A reverse proxy or tunnel is sending the upgrade to "
                    "the wrong one. Route the ROOT path to the gateway and "
                    "everything else here — the relay's WebSocket URL carries "
                    "no path, every API call does. See deploy/README.md."
                ),
            },
            status=426,   # Upgrade Required
            headers={"Sec-WebSocket-Version": "13"},
        )
    return web.json_response({"service": "openrecall", "api": "http-control"})


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