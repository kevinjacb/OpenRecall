"""Phase 4 HTTP — POST /commands (spec §4.3, blocking gap #5).

Until this existed, commands could only be created by the agent's internal
``issue_command`` path, so the Settings toggles had nothing to call.
"""
from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from opensapien_server.agent.capability import ConstantCapabilityProvider
from opensapien_server.commands.dispatcher import CommandDispatcher
from opensapien_server.commands.signing import CommandSigner
from opensapien_server.commands.store import SqliteCommandStore
from opensapien_server.contracts.clock import FakeClock
from opensapien_server.contracts.id_generator import DeterministicIdGenerator
from opensapien_server.contracts.types import CapabilitySet, DeviceResourceStatus
from opensapien_server.http.app import build_app

_AUTH = {"Authorization": "Bearer t"}


def _client(tmp_path, *, capabilities=None, resources=None):
    clock = FakeClock()
    store = SqliteCommandStore(tmp_path / "commands.db")
    dispatcher = CommandDispatcher(
        CommandSigner.generate(), clock=clock.now, store=store,
    )
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        command_store=store,
        command_dispatcher=dispatcher,
        id_generator=DeterministicIdGenerator(),
        clock=clock,
        capability_provider=ConstantCapabilityProvider(
            capabilities=capabilities,
            resources=resources or DeviceResourceStatus(relay_connected=True),
        ),
    )
    return TestClient(TestServer(app)), dispatcher, store


async def _post(tmp_path, payload, **kwargs):
    client, dispatcher, store = _client(tmp_path, **kwargs)
    async with client:
        resp = await client.post("/commands", json=payload, headers=_AUTH)
        return resp.status, await resp.json(), dispatcher


# ---- the happy path ---------------------------------------------------------


async def test_creating_a_command_returns_201_and_the_record(tmp_path):
    status, body, dispatcher = await _post(
        tmp_path, {"type": "start_audio", "idempotency_key": "k1"},
    )

    assert status == 201
    assert body["type"] == "start_audio"
    assert body["status"] == "PENDING"
    assert body["command_id"]
    assert [c.command.type for c in dispatcher.pending()] == ["start_audio"]


async def test_a_created_command_is_signed(tmp_path):
    client, dispatcher, _store = _client(tmp_path)
    async with client:
        await client.post(
            "/commands", json={"type": "stop_audio", "idempotency_key": "k1"},
            headers=_AUTH,
        )

    (signed,) = dispatcher.pending()
    assert signed.signature


async def test_a_command_is_unbound_by_default(tmp_path):
    """An HTTP caller has no truthful session id to supply (spec D3) — an
    empty one means "the device, whenever it is next connected"."""
    status, body, _dispatcher = await _post(
        tmp_path, {"type": "start_audio", "idempotency_key": "k1"},
    )

    assert body["session_id"] == ""


async def test_params_are_passed_through(tmp_path):
    status, body, _dispatcher = await _post(
        tmp_path,
        {"type": "request_buffer", "params": {"seconds": 10}, "idempotency_key": "k1"},
    )

    assert status == 201
    assert body["params"] == {"seconds": 10}


# ---- idempotency ------------------------------------------------------------


async def test_a_replayed_key_returns_the_original_at_200(tmp_path):
    """The client can tell "your retry was absorbed" from "a second command
    is now in flight"."""
    client, dispatcher, _store = _client(tmp_path)
    async with client:
        first = await client.post(
            "/commands", json={"type": "start_audio", "idempotency_key": "k1"},
            headers=_AUTH,
        )
        first_body = await first.json()
        second = await client.post(
            "/commands", json={"type": "start_audio", "idempotency_key": "k1"},
            headers=_AUTH,
        )
        second_body = await second.json()

    assert first.status == 201
    assert second.status == 200
    assert first_body["command_id"] == second_body["command_id"]
    assert len(dispatcher.pending()) == 1


async def test_a_different_key_creates_a_second_command(tmp_path):
    client, dispatcher, _store = _client(tmp_path)
    async with client:
        await client.post(
            "/commands", json={"type": "start_audio", "idempotency_key": "k1"},
            headers=_AUTH,
        )
        await client.post(
            "/commands", json={"type": "start_audio", "idempotency_key": "k2"},
            headers=_AUTH,
        )

    assert len(dispatcher.pending()) == 2


# ---- rejection --------------------------------------------------------------


async def test_an_unknown_command_type_is_400(tmp_path):
    """The allowlist is the binding claim that firmware knows how to execute
    a type."""
    status, body, _dispatcher = await _post(
        tmp_path, {"type": "self_destruct", "idempotency_key": "k1"},
    )

    assert status == 400
    assert body["code"] == "bad_request"


async def test_a_bad_param_is_400(tmp_path):
    status, body, _dispatcher = await _post(
        tmp_path,
        {"type": "request_buffer", "params": {"seconds": 9999},
         "idempotency_key": "k1"},
    )

    assert status == 400


async def test_a_missing_required_param_is_400(tmp_path):
    status, _body, _dispatcher = await _post(
        tmp_path, {"type": "request_buffer", "idempotency_key": "k1"},
    )

    assert status == 400


async def test_a_missing_idempotency_key_is_400(tmp_path):
    """Required, not generated: only the caller knows which retries are the
    same request."""
    status, body, _dispatcher = await _post(tmp_path, {"type": "start_audio"})

    assert status == 400
    assert "idempotency_key" in body["message"]


async def test_a_missing_type_is_400(tmp_path):
    status, _body, _dispatcher = await _post(tmp_path, {"idempotency_key": "k1"})

    assert status == 400


async def test_non_object_params_are_400(tmp_path):
    status, _body, _dispatcher = await _post(
        tmp_path,
        {"type": "start_audio", "params": "nope", "idempotency_key": "k1"},
    )

    assert status == 400


async def test_a_guardrail_refusal_is_403_not_400(tmp_path):
    """"The device can't do that right now" is a different problem from
    "your request was malformed", and a client needs to tell them apart."""
    status, body, dispatcher = await _post(
        tmp_path,
        {"type": "capture_photo", "idempotency_key": "k1"},
        capabilities=CapabilitySet(camera=False),
    )

    assert status == 403
    assert body["code"] == "forbidden"
    assert dispatcher.pending() == []


async def test_a_disconnected_device_refuses_at_403(tmp_path):
    status, _body, dispatcher = await _post(
        tmp_path,
        {"type": "start_audio", "idempotency_key": "k1"},
        resources=DeviceResourceStatus(relay_connected=False),
    )

    assert status == 403
    assert dispatcher.pending() == []


async def test_a_non_json_body_is_400(tmp_path):
    client, _dispatcher, _store = _client(tmp_path)
    async with client:
        resp = await client.post("/commands", data="not json", headers=_AUTH)

    assert resp.status == 400


async def test_creating_a_command_requires_a_token(tmp_path):
    client, _dispatcher, _store = _client(tmp_path)
    async with client:
        resp = await client.post(
            "/commands", json={"type": "start_audio", "idempotency_key": "k1"},
        )

    assert resp.status == 401
