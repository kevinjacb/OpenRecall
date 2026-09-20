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


def test_the_http_api_has_no_root_route():
    """Nothing may be served at "/" on the HTTP API.

    The tunnel gives the root path to the WebSocket gateway, so an HTTP route
    at "/" would become unreachable remotely while still working locally —
    the worst kind of divergence to debug.
    """
    routes_dir = REPO / "server/src/openrecall_server/http/routes"
    offenders = [
        f"{p.name}: {line.strip()}"
        for p in routes_dir.glob("*.py")
        for line in p.read_text().splitlines()
        if 'add_get("/")' in line or 'add_post("/")' in line
    ]
    assert not offenders, (
        f"the HTTP API registers a root route ({offenders}); the tunnel config "
        "routes / to the WebSocket gateway, so it would be unreachable remotely")
