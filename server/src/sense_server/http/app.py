from aiohttp import web

from sense_server.http.auth import bearer_auth_middleware


def build_app(*, token, get_pubkey):
    app = web.Application(middlewares=[bearer_auth_middleware])
    app["sense_token"] = token
    app["sense_get_pubkey"] = get_pubkey
    from sense_server.http.routes.provisioning import add_routes

    add_routes(app)
    return app