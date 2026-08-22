"""Phase 4 HTTP — GET / PUT /settings (spec §4.1, §4.2)."""
from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.http.app import build_app
from openrecall_server.settings.model import SettingsDocument
from openrecall_server.settings.store import InMemorySettingsStore

_AUTH = {"Authorization": "Bearer t"}


class RecordingReconciler:
    def __init__(self):
        self.calls = 0

    def reconcile(self):
        self.calls += 1
        return None


def _client(document=None, reconciler=None):
    store = InMemorySettingsStore(document)
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        settings_store=store,
        reconciler=reconciler,
    )
    return TestClient(TestServer(app)), store


# ---- GET --------------------------------------------------------------------


async def test_get_returns_the_default_document():
    client, _store = _client()
    async with client:
        resp = await client.get("/settings", headers=_AUTH)
        body = await resp.json()

    assert resp.status == 200
    assert body == {
        "schema_version": "v1",
        "capture": {
            "audio_enabled": True, "save_audio": True, "vision_enabled": False,
            "sleep_mode": False,
            "snapshot_interval_s": 60,
        },
        "retention": {"audio_days": 30, "snapshot_days": 30},
    }


async def test_sleep_mode_defaults_false_in_get():
    client, _store = _client()
    async with client:
        resp = await client.get("/settings", headers=_AUTH)
        body = await resp.json()

    assert resp.status == 200
    assert body["capture"]["sleep_mode"] is False


async def test_put_sleep_mode_persists_and_round_trips():
    client, store = _client()
    async with client:
        resp = await client.put(
            "/settings", json={"capture": {"sleep_mode": True}}, headers=_AUTH,
        )
        body = await resp.json()
        assert resp.status == 200
        assert body["capture"]["sleep_mode"] is True

        resp = await client.get("/settings", headers=_AUTH)
        body = await resp.json()
        assert body["capture"]["sleep_mode"] is True

    assert store.get().capture.sleep_mode is True


async def test_get_requires_a_token():
    client, _store = _client()
    async with client:
        resp = await client.get("/settings")
    assert resp.status == 401


async def test_get_without_a_store_is_404():
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/settings", headers=_AUTH)
    assert resp.status == 404


# ---- PUT --------------------------------------------------------------------


async def test_put_applies_a_partial_patch():
    client, store = _client()
    async with client:
        resp = await client.put(
            "/settings", json={"capture": {"vision_enabled": True}}, headers=_AUTH,
        )
        body = await resp.json()

    assert resp.status == 200
    assert body["capture"]["vision_enabled"] is True
    assert body["capture"]["audio_enabled"] is True  # untouched
    assert store.get().capture.vision_enabled is True


async def test_put_persists_across_requests():
    client, _store = _client()
    async with client:
        await client.put(
            "/settings", json={"retention": {"audio_days": 7}}, headers=_AUTH,
        )
        body = await (await client.get("/settings", headers=_AUTH)).json()

    assert body["retention"]["audio_days"] == 7


async def test_put_rejects_an_unknown_key():
    """A typo must not read as a successful change."""
    client, _store = _client()
    async with client:
        resp = await client.put(
            "/settings", json={"capture": {"audio_enabld": False}}, headers=_AUTH,
        )
        body = await resp.json()

    assert resp.status == 400
    assert body["code"] == "bad_request"


async def test_put_rejects_a_wrong_type():
    client, _store = _client()
    async with client:
        resp = await client.put(
            "/settings", json={"retention": {"audio_days": "lots"}}, headers=_AUTH,
        )

    assert resp.status == 400


async def test_put_rejects_a_non_json_body():
    client, _store = _client()
    async with client:
        resp = await client.put(
            "/settings", data="not json", headers=_AUTH,
        )

    assert resp.status == 400


async def test_put_requires_a_token():
    client, _store = _client()
    async with client:
        resp = await client.put("/settings", json={})
    assert resp.status == 401


# ---- P3: snapshot_interval_s + snapshot_days -------------------------------


async def test_put_snapshot_interval_and_snapshot_days_round_trip():
    client = _client()[0]
    async with client:
        resp = await client.put(
            "/settings",
            json={
                "capture": {"snapshot_interval_s": 120},
                "retention": {"snapshot_days": 7},
            },
            headers=_AUTH,
        )
        body = await resp.json()
        assert resp.status == 200
        assert body["capture"]["snapshot_interval_s"] == 120
        assert body["retention"]["snapshot_days"] == 7
        # unset fields are preserved
        assert body["capture"]["sleep_mode"] is False


async def test_put_rejects_snapshot_interval_s_out_of_range():
    client, _store = _client()
    async with client:
        resp = await client.put(
            "/settings",
            json={"capture": {"snapshot_interval_s": 601}},
            headers=_AUTH,
        )
    assert resp.status == 400


async def test_put_rejects_snapshot_days_negative():
    client, _store = _client()
    async with client:
        resp = await client.put(
            "/settings",
            json={"retention": {"snapshot_days": -1}},
            headers=_AUTH,
        )
    assert resp.status == 400


# ---- reconciliation on change (§4.2) ----------------------------------------


async def test_changing_audio_enabled_reconciles_immediately():
    """If the device happens to be connected, the toggle should take effect
    now rather than at the next hello."""
    reconciler = RecordingReconciler()
    client, _store = _client(reconciler=reconciler)
    async with client:
        await client.put(
            "/settings", json={"capture": {"audio_enabled": False}}, headers=_AUTH,
        )

    assert reconciler.calls == 1


async def test_an_unchanged_audio_enabled_does_not_reconcile():
    reconciler = RecordingReconciler()
    client, _store = _client(reconciler=reconciler)
    async with client:
        await client.put(
            "/settings", json={"capture": {"audio_enabled": True}}, headers=_AUTH,
        )

    assert reconciler.calls == 0


async def test_a_server_side_only_setting_does_not_reconcile():
    """`save_audio` never leaves the server, so it has no device state to
    converge."""
    reconciler = RecordingReconciler()
    client, _store = _client(reconciler=reconciler)
    async with client:
        await client.put(
            "/settings", json={"capture": {"save_audio": False}}, headers=_AUTH,
        )

    assert reconciler.calls == 0


async def test_a_failing_reconciler_does_not_lose_the_setting():
    class Exploding:
        def reconcile(self):
            raise RuntimeError("no device")

    client, store = _client(reconciler=Exploding())
    async with client:
        resp = await client.put(
            "/settings", json={"capture": {"audio_enabled": False}}, headers=_AUTH,
        )

    assert resp.status == 200
    assert store.get().capture.audio_enabled is False


async def test_put_without_a_reconciler_still_saves():
    client, store = _client(
        SettingsDocument.model_validate({"capture": {"audio_enabled": True}}),
    )
    async with client:
        resp = await client.put(
            "/settings", json={"capture": {"audio_enabled": False}}, headers=_AUTH,
        )

    assert resp.status == 200
    assert store.get().capture.audio_enabled is False
