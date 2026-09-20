"""Cross-tier invariants the documented Cloudflare Tunnel config depends on.

The tunnel routes the ROOT path to the WebSocket gateway and every other path
to the HTTP API (deploy/README.md). That works only because of two facts that
live in two different tiers, neither of which is obviously load-bearing where
it is written. If either changes, remote access breaks in a way that looks
like a Cloudflare problem rather than a code change.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WS_URL_KT = (REPO / "android/openrecall-relay/app/src/main/kotlin/com/openrecall"
                    "/relay/net/WsUrl.kt")


def test_the_relay_still_connects_to_the_root_path():
    """The Android relay must not put a path on the WebSocket URL.

    `wsGatewayUrl` builds "$scheme://$host[:$port]" and deliberately drops path
    and query. The tunnel's `path: ^/$` rule is what separates WS traffic from
    HTTP API traffic on a single hostname, so a path here would route the
    WebSocket to the HTTP API and fail the 101 upgrade.
    """
    src = WS_URL_KT.read_text()
    assert 'return if (port != null) "$scheme://$host:$port" else "$scheme://$host"' in src, (
        "wsGatewayUrl no longer returns a bare scheme://host[:port]. If it now "
        "appends a path, update the `path: ^/$` ingress rule in "
        "deploy/README.md to match, or the tunnel will route the WebSocket to "
        "the HTTP API.")


def test_the_only_root_route_is_the_misconfiguration_diagnostic():
    """The root path belongs to the WebSocket gateway, not the HTTP API.

    The tunnel routes "/" to the gateway, so any *functional* HTTP route there
    would work locally and be unreachable remotely — the worst kind of
    divergence. The one permitted exception is the diagnostic in
    `provisioning.root`, which exists precisely for the case where the routing
    is wrong and is therefore unreachable when it is right.

    (An earlier version of this test searched for the literal `add_get("/")`,
    which never matches a real registration — those read `add_get("/", handler)`
    — so it was vacuous. It is asserted against the router here instead of
    against the source text.)
    """
    from openrecall_server.http.app import build_app

    app = build_app(token=None, get_pubkey=lambda: "00" * 32)
    root_handlers = {
        getattr(r.handler, "__name__", repr(r.handler))
        for r in app.router.routes()
        if getattr(getattr(r, "resource", None), "canonical", None) == "/"
    }
    assert root_handlers <= {"root"}, (
        f"unexpected handler(s) registered at /: {root_handlers - {'root'}}. "
        "The tunnel gives the root path to the WebSocket gateway, so anything "
        "functional there is unreachable remotely."
    )


def test_the_root_diagnostic_answers_a_websocket_upgrade(aiohttp_or_none=None):
    """A WebSocket upgrade arriving at the HTTP API must explain itself.

    aiohttp answers an unrouted upgrade with 404, which clients report as
    "Expected HTTP 101 response" — neither names the cause, so people debug
    the WebSocket code instead of their proxy.
    """
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from openrecall_server.http.app import build_app

    async def check():
        app = build_app(token=None, get_pubkey=lambda: "00" * 32,
                        gateway_port=8765)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/", headers={"Upgrade": "websocket",
                                                  "Connection": "Upgrade"})
            assert resp.status == 426, resp.status
            body = await resp.json()
            assert body["error"] == "websocket_sent_to_http_api"
            assert "8765" in body["detail"], "should name the gateway port"

            plain = await client.get("/")
            assert plain.status == 200
            assert (await plain.json())["api"] == "http-control"
        finally:
            await client.close()

    asyncio.run(check())
