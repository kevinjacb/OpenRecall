"""Tests for the hermes/relay bearer principal and route-class gating.

The brief specifies the ``aiohttp_client`` pytest fixture, but this codebase
does not install ``pytest-aiohttp``; existing HTTP tests use
``aiohttp.test_utils.TestClient/TestServer`` directly (see
``tests/http/test_reminders_routes.py``). The test bodies (assertions,
status codes) are verbatim from the brief; only the client-construction
mechanism is adapted to the codebase pattern.
"""
from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.http.app import build_app
from openrecall_server.mcp.principal import (
    PRINCIPAL_HERMES, PRINCIPAL_RELAY, principal_for,
)


class _Req(dict):
    def __init__(self, auth):
        super().__init__()
        self.headers = {"Authorization": auth}


def build_test_app(*, relay_token, hermes_token):
    return build_app(
        token=relay_token,
        hermes_token=hermes_token,
        get_pubkey=lambda: bytes(32),
    )


async def _client(app):
    cli = TestClient(TestServer(app))
    await cli.start_server()
    return cli


def test_relay_token_maps_to_relay_principal():
    assert principal_for(_Req("Bearer aaa"), relay_token="aaa",
                         hermes_token="bbb") == PRINCIPAL_RELAY


def test_hermes_token_maps_to_hermes_principal():
    assert principal_for(_Req("Bearer bbb"), relay_token="aaa",
                         hermes_token="bbb") == PRINCIPAL_HERMES


def test_unknown_token_maps_to_none():
    assert principal_for(_Req("Bearer zzz"), relay_token="aaa",
                         hermes_token="bbb") is None


async def test_relay_token_cannot_reach_mcp():
    app = build_test_app(relay_token="aaa", hermes_token="bbb")
    client = await _client(app)
    try:
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                            "method": "ping"},
                              headers={"Authorization": "Bearer aaa"})
        assert r.status == 403
    finally:
        await client.close()


async def test_hermes_token_cannot_reach_memory_route():
    app = build_test_app(relay_token="aaa", hermes_token="bbb")
    client = await _client(app)
    try:
        r = await client.get("/memory?q=x", headers={"Authorization": "Bearer bbb"})
        assert r.status == 403
    finally:
        await client.close()
