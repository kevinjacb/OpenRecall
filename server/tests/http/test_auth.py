import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sense_server.http.app import build_app


@pytest.fixture
async def client(tmp_path):
    from sense_server.auth import load_or_create_token
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(token=token, get_pubkey=lambda: bytes(32))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, token
    await client.close()


async def test_health_requires_token(client):
    cli, token = client
    resp = await cli.get("/health")
    assert resp.status == 401


async def test_health_ok_with_token(client):
    cli, token = client
    resp = await cli.get("/health", headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    # No gateway configured in this fixture → field omitted (back-compat).
    assert "gatewayPort" not in body


async def test_health_advertises_gateway_port(tmp_path):
    from sense_server.auth import load_or_create_token
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(token=token, get_pubkey=lambda: bytes(32), gateway_port=8765)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        resp = await cli.get("/health", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["gatewayPort"] == 8765
    finally:
        await cli.close()


async def test_rejects_wrong_token(client):
    cli, _ = client
    resp = await cli.get("/health", headers={"Authorization": "Bearer deadbeef"})
    assert resp.status == 401