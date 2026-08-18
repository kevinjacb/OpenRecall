"""Tests for GET /reminders + POST /reminders/{id}/done (P1 instruction processor).

The brief specifies the ``aiohttp_client`` pytest fixture, but this codebase
does not install ``pytest-aiohttp``; existing HTTP tests use
``aiohttp.test_utils.TestClient/TestServer`` directly. The test bodies
(assertions, JSON shapes, status codes) are verbatim from the brief; only
the client-construction mechanism is adapted to the codebase pattern.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.http.app import build_app
from openrecall_server.reminders.store import InMemoryReminderStore


def _due(minute):
    return datetime(2026, 8, 18, 12, minute, 0, tzinfo=timezone.utc)


def _app(store=None):
    return build_app(
        token="t", get_pubkey=lambda: b"\x00" * 32, reminders=store or InMemoryReminderStore(),
    )


async def _client(app):
    cli = TestClient(TestServer(app))
    await cli.start_server()
    return cli


async def test_get_reminders_returns_pending_whole_dict():
    store = InMemoryReminderStore()
    store.add(atom_id="r1", session_id="s1", text="Call mom", due_at=_due(0))
    store.add(atom_id="r2", session_id="s1", text="Buy milk", due_at=_due(30))
    store.mark_done("r2")
    cli = await _client(_app(store))
    try:
        resp = await cli.get("/reminders", headers={"Authorization": "Bearer t"})
        assert resp.status == 200
        body = await resp.json()
        assert body == {
            "schema_version": "v1",
            "reminders": [
                {
                    "atom_id": "r1", "session_id": "s1", "text": "Call mom",
                    "due_at": _due(0).isoformat(), "status": "pending", "fired_at": None,
                },
            ],
        }
    finally:
        await cli.close()


async def test_get_reminders_requires_bearer():
    cli = await _client(_app())
    try:
        resp = await cli.get("/reminders")
        assert resp.status == 401
    finally:
        await cli.close()


async def test_post_reminders_done_marks_done():
    store = InMemoryReminderStore()
    store.add(atom_id="r1", session_id="s1", text="Call mom", due_at=_due(0))
    cli = await _client(_app(store))
    try:
        resp = await cli.post("/reminders/r1/done", headers={"Authorization": "Bearer t"}, json={})
        assert resp.status == 200
        assert store.get("r1").status == "done"
    finally:
        await cli.close()


async def test_post_reminders_done_unknown_returns_404():
    cli = await _client(_app())
    try:
        resp = await cli.post("/reminders/nope/done", headers={"Authorization": "Bearer t"}, json={})
        assert resp.status == 404
    finally:
        await cli.close()


async def test_post_reminders_done_requires_bearer():
    cli = await _client(_app())
    try:
        resp = await cli.post("/reminders/r1/done", json={})
        assert resp.status == 401
    finally:
        await cli.close()