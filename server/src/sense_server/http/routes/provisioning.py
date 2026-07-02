from aiohttp import web


def add_routes(app):
    app.router.add_get("/health", health)


async def health(request):
    if not request.get("authorized"):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"status": "ok"})