from aiohttp import web


def add_routes(app):
    app.router.add_get("/health", health)
    app.router.add_get("/provisioning/pubkey", provisioning_pubkey)


async def health(request):
    return web.json_response({"status": "ok"})


async def provisioning_pubkey(request):
    get_pubkey = request.app["sense_get_pubkey"]
    pk = get_pubkey()
    if pk is None:
        return web.json_response({"error": "no_key"}, status=503)
    return web.json_response({"pubkey": pk.hex(), "key_id": "default", "created_at": None})