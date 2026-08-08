"""Tests for the HTTP /status route."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.auth import load_or_create_token
from openrecall_server.events.model import CaptureEvent
from openrecall_server.http.app import build_app
from openrecall_server.sessions.index import SessionIndex
from openrecall_server.sessions.lifecycle import SessionLifecycle


async def _client(tmp_path, *, index=None, lifecycle=None, store=None):
    """Build a fully-wired app + token for HTTP tests."""
    token = load_or_create_token(tmp_path / "tok")
    if index is None:
        index = SessionIndex()
    if lifecycle is None:
        lifecycle = SessionLifecycle()
    app = build_app(
        token=token,
        get_pubkey=lambda: bytes(32),
        session_index=index,
        session_lifecycle=lifecycle,
        event_store=store,
    )
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    return cli, token, index, lifecycle


# ---- auth -------------------------------------------------------------------


async def test_status_requires_token(tmp_path):
    cli, _, _, _ = await _client(tmp_path)
    try:
        resp = await cli.get("/status")
        assert resp.status == 401
    finally:
        await cli.close()


# ---- shape + fields ---------------------------------------------------------


async def test_status_returns_expected_shape(tmp_path):
    cli, token, _, _ = await _client(tmp_path)
    try:
        resp = await cli.get("/status", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert set(body.keys()) == {
            "reachable",
            "authenticated",
            "version",
            "uptimeSeconds",
            "activeSessions",
            "totalSessions",
            "recentEvents24h",
        }
        assert body["reachable"] is True
        assert body["authenticated"] is True
        assert isinstance(body["version"], str) and body["version"]
        assert isinstance(body["uptimeSeconds"], int)
        assert body["uptimeSeconds"] >= 0
        assert body["activeSessions"] == 0
        assert body["totalSessions"] == 0
        assert body["recentEvents24h"] == 0
    finally:
        await cli.close()


# ---- counters ---------------------------------------------------------------


async def test_status_total_sessions_reflects_index(tmp_path):
    cli, token, index, _ = await _client(tmp_path)
    try:
        for i in range(2):
            index.record(
                CaptureEvent(
                    event_id=f"s{i}:0",
                    session_id=f"s{i}",
                    seq=0,
                    kind="transcript",
                    created_at=datetime.now(timezone.utc),
                    text=f"t{i}",
                    duration_ms=100,
                    start_ms=0,
                )
            )
        resp = await cli.get("/status", headers={"Authorization": f"Bearer {token}"})
        body = await resp.json()
        assert body["totalSessions"] == 2
    finally:
        await cli.close()


async def test_status_recent_events_24h_reflects_index(tmp_path):
    cli, token, index, _ = await _client(tmp_path)
    try:
        for i in range(3):
            index.record(
                CaptureEvent(
                    event_id=f"s{i}:0",
                    session_id=f"s{i}",
                    seq=0,
                    kind="transcript",
                    created_at=datetime.now(timezone.utc),
                    text=f"t{i}",
                    duration_ms=100,
                    start_ms=0,
                )
            )
        resp = await cli.get("/status", headers={"Authorization": f"Bearer {token}"})
        body = await resp.json()
        assert body["recentEvents24h"] == 3
    finally:
        await cli.close()


async def test_status_active_sessions_reflects_lifecycle(tmp_path):
    cli, token, _, lifecycle = await _client(tmp_path)
    try:
        lifecycle.register("s1")
        resp = await cli.get("/status", headers={"Authorization": f"Bearer {token}"})
        body = await resp.json()
        assert body["activeSessions"] == 1
        lifecycle.deregister("s1")
        resp = await cli.get("/status", headers={"Authorization": f"Bearer {token}"})
        body = await resp.json()
        assert body["activeSessions"] == 0
    finally:
        await cli.close()


# ---- uptime -----------------------------------------------------------------


async def test_status_uptime_is_monotonic(tmp_path):
    cli, token, _, _ = await _client(tmp_path)
    try:
        resp1 = await cli.get("/status", headers={"Authorization": f"Bearer {token}"})
        u1 = (await resp1.json())["uptimeSeconds"]
        # Sleep 10ms so the next reading is strictly greater (or at least
        # not smaller — tolerance for coarse clock resolution)
        await asyncio.sleep(0.01)
        resp2 = await cli.get("/status", headers={"Authorization": f"Bearer {token}"})
        u2 = (await resp2.json())["uptimeSeconds"]
        assert u2 >= u1
    finally:
        await cli.close()
