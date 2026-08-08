from aiohttp.test_utils import TestClient, TestServer
from opensapien_server.http.app import build_app
from opensapien_server.auth import load_or_create_token


async def _client(tmp_path, get_pubkey):
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(token=token, get_pubkey=get_pubkey)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    return cli, token


async def test_pubkey_returns_hex(tmp_path):
    cli, token = await _client(tmp_path, lambda: bytes(range(32)))
    resp = await cli.get("/provisioning/pubkey",
                        headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 200
    body = await resp.json()
    assert body["pubkey"] == bytes(range(32)).hex()
    assert body["key_id"] == "default"
    await cli.close()


async def test_pubkey_503_when_no_key(tmp_path):
    cli, token = await _client(tmp_path, lambda: None)
    resp = await cli.get("/provisioning/pubkey",
                        headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 503
    assert (await resp.json())["error"] == "no_key"
    await cli.close()


async def test_pubkey_requires_token(tmp_path):
    cli, _ = await _client(tmp_path, lambda: bytes(32))
    resp = await cli.get("/provisioning/pubkey")
    assert resp.status == 401
    await cli.close()