"""Tests for the HTTP /sessions routes.

These tests follow the same aiohttp `TestClient` pattern as
`test_provisioning.py`. The index and event store are wired in by hand
(via `app[...]`) — the full `build_app` is exercised separately.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sense_server.auth import load_or_create_token
from sense_server.events.model import CaptureEvent
from sense_server.events.store import InMemoryEventStore
from sense_server.http.app import build_app
from sense_server.sessions.index import SessionIndex
from sense_server.sessions.lifecycle import SessionLifecycle


# ---- helpers ----------------------------------------------------------------


def _ce(session_id: str, seq: int, text: str = "hello", **kwargs) -> CaptureEvent:
    """Build a capture event for tests; default time is monotonic so order is preserved."""
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=kwargs.pop(
            "created_at",
            datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        ),
        text=text,
        duration_ms=100,
        start_ms=seq * 100,
        **kwargs,
    )


async def _client(tmp_path):
    """Build a fully-wired app + token for HTTP tests."""
    token = load_or_create_token(tmp_path / "tok")
    index = SessionIndex()
    store = InMemoryEventStore()
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
    return cli, token, index, store, lifecycle


# ---- /sessions list ---------------------------------------------------------


async def test_sessions_requires_token(tmp_path):
    cli, _, _, _, _ = await _client(tmp_path)
    try:
        resp = await cli.get("/sessions")
        assert resp.status == 401
    finally:
        await cli.close()


async def test_sessions_empty_returns_empty_page(tmp_path):
    cli, token, _, _, _ = await _client(tmp_path)
    try:
        resp = await cli.get("/sessions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert body == {"sessions": [], "nextCursor": None}
    finally:
        await cli.close()


async def test_sessions_paginates_25_sessions(tmp_path):
    cli, token, index, _, _ = await _client(tmp_path)
    try:
        base = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
        for i in range(25):
            index.record(_ce(f"s{i:03d}", 0, text=f"session {i}",
                             created_at=base.replace(minute=i)))

        resp = await cli.get(
            "/sessions?limit=10", headers={"Authorization": f"Bearer {token}"}
        )
        body = await resp.json()
        assert resp.status == 200
        assert len(body["sessions"]) == 10
        assert body["nextCursor"] is not None
        # Newest first
        assert body["sessions"][0]["id"] == "s024"
        assert body["sessions"][-1]["id"] == "s015"

        resp2 = await cli.get(
            f"/sessions?limit=10&cursor={body['nextCursor']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        body2 = await resp2.json()
        assert len(body2["sessions"]) == 10
        assert body2["nextCursor"] is not None
        assert body2["sessions"][0]["id"] == "s014"
        assert body2["sessions"][-1]["id"] == "s005"

        resp3 = await cli.get(
            f"/sessions?limit=10&cursor={body2['nextCursor']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        body3 = await resp3.json()
        assert len(body3["sessions"]) == 5
        assert body3["nextCursor"] is None
        assert body3["sessions"][-1]["id"] == "s000"
    finally:
        await cli.close()


async def test_sessions_summary_shape_matches_dto(tmp_path):
    cli, token, index, _, _ = await _client(tmp_path)
    try:
        index.record(
            _ce(
                "s1",
                0,
                text="hello world",
                created_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
            )
        )
        resp = await cli.get("/sessions", headers={"Authorization": f"Bearer {token}"})
        body = await resp.json()
        s = body["sessions"][0]
        # Wire shape must match the Android DTO field-for-field (camelCase keys).
        assert set(s.keys()) == {
            "id", "startedAt", "endedAt", "durationMs", "transcriptCount", "preview"
        }
        assert s["id"] == "s1"
        assert s["startedAt"] == "2026-07-01T12:00:00+00:00"
        assert s["endedAt"] is None
        assert s["durationMs"] >= 0
        assert s["transcriptCount"] == 1
        assert s["preview"] == "hello world"
    finally:
        await cli.close()


async def test_sessions_malformed_cursor_returns_400(tmp_path):
    cli, token, _, _, _ = await _client(tmp_path)
    try:
        resp = await cli.get(
            "/sessions?cursor=not-a-real-cursor",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "bad_cursor"
    finally:
        await cli.close()


async def test_sessions_limit_is_clamped_not_rejected(tmp_path):
    """Out-of-range limit is clamped (1 <= limit <= 100), not a 400."""
    cli, token, index, _, _ = await _client(tmp_path)
    try:
        index.record(_ce("s1", 0))
        # limit=0 -> clamped to 1 (still 200, not 400)
        resp = await cli.get(
            "/sessions?limit=0", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 200
        # limit=99999 -> clamped to 100
        resp = await cli.get(
            "/sessions?limit=99999", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 200
    finally:
        await cli.close()


# ---- /sessions/{id} ---------------------------------------------------------


async def test_session_detail_returns_summary_and_events(tmp_path):
    cli, token, index, store, _ = await _client(tmp_path)
    try:
        # Use real timestamping so we can assert startedAt matches the first event
        t0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        for seq, text in enumerate(["a", "b", "c"]):
            e = CaptureEvent(
                event_id=f"s1:{seq}",
                session_id="s1",
                seq=seq,
                kind="transcript",
                created_at=t0,
                text=text,
                duration_ms=100,
                start_ms=seq * 100,
            )
            store.append(e)
            index.record(e)

        resp = await cli.get(
            "/sessions/s1", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert set(body.keys()) == {"summary", "events"}
        assert body["summary"]["id"] == "s1"
        assert body["summary"]["startedAt"] == "2026-07-01T12:00:00+00:00"
        assert body["summary"]["endedAt"] is None  # no Bye yet
        assert body["summary"]["transcriptCount"] == 3
        # Events come back in seq order with the wire shape that matches the DTO
        assert [e["id"] for e in body["events"]] == ["s1:0", "s1:1", "s1:2"]
        assert body["events"][0]["sessionId"] == "s1"
        assert body["events"][0]["kind"] == "transcript"
        assert body["events"][0]["text"] == "a"
    finally:
        await cli.close()


async def test_session_detail_unknown_returns_404(tmp_path):
    cli, token, _, _, _ = await _client(tmp_path)
    try:
        resp = await cli.get(
            "/sessions/unknown", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 404
    finally:
        await cli.close()


# ---- /sessions/{id}/events --------------------------------------------------


async def test_session_events_returns_event_list(tmp_path):
    cli, token, index, store, _ = await _client(tmp_path)
    try:
        for seq in range(3):
            e = _ce("s1", seq)
            store.append(e)
            index.record(e)
        resp = await cli.get(
            "/sessions/s1/events", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"events": body["events"]}  # shape check
        assert len(body["events"]) == 3
    finally:
        await cli.close()


async def test_session_events_unknown_returns_404(tmp_path):
    cli, token, _, _, _ = await _client(tmp_path)
    try:
        resp = await cli.get(
            "/sessions/unknown/events", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 404
    finally:
        await cli.close()


async def test_session_events_empty_session_returns_200_empty_list(tmp_path):
    """A known session with no events: 200 + empty list, not 404."""
    cli, token, index, _, _ = await _client(tmp_path)
    try:
        index.record(_ce("empty", 0))  # creates the summary
        # But we never added an event to the store for "empty", so the store has no
        # events. The route should distinguish "known session" (200 empty) from
        # "unknown session" (404). We achieve this by routing on the index first.
        resp = await cli.get(
            "/sessions/empty/events", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"events": []}
    finally:
        await cli.close()
