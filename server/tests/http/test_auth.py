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
    assert (await resp.json())["status"] == "ok"


async def test_rejects_wrong_token(client):
    cli, _ = client
    resp = await cli.get("/health", headers={"Authorization": "Bearer deadbeef"})
    assert resp.status == 401