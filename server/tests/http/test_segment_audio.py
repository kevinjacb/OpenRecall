"""Phase 3 HTTP — /segments/{id}/audio and /waveform (spec §3.2, §3.3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.events.model import CaptureEvent
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.http.app import build_app
from openrecall_server.media.audio import AudioStore
from openrecall_server.sessions.segments import SegmentIndex

_T0 = datetime(2026, 8, 8, 9, 0, 0, tzinfo=timezone.utc)
_AUTH = {"Authorization": "Bearer t"}


def _event(seq, *, session_id="s1", at=None, ms=1000):
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=at if at is not None else _T0 + timedelta(seconds=seq),
        text="hello",
        duration_ms=ms,
        start_ms=seq * ms,
    )


def _client(tmp_path, *, events=(), frames=None, peaks=None, closed=True):
    event_store = InMemoryEventStore()
    for e in events:
        event_store.append(e)
    index = SegmentIndex()
    index.rebuild_from_store(event_store)
    if closed:
        index.close_session("s1")
    audio = AudioStore(tmp_path / "audio")
    if frames is not None:
        audio.write_at("s1", 0, frames, peaks=peaks)
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        event_store=event_store,
        segment_index=index,
        audio_store=audio,
    )
    return TestClient(TestServer(app)), audio, index


# ---- /audio -----------------------------------------------------------------


async def test_audio_serves_an_ogg_stream(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0), _event(1)], frames=[b"aa"] * 100,
    )
    async with client:
        resp = await client.get("/segments/s1:0/audio", headers=_AUTH)
        body = await resp.read()

    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("audio/ogg")
    assert body.startswith(b"OggS")
    assert b"OpusHead" in body[:100]


async def test_audio_supports_range_requests(tmp_path):
    """Scrubbing depends on this — without Range the client must download
    the whole recording to seek."""
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 100,
    )
    async with client:
        resp = await client.get(
            "/segments/s1:0/audio", headers={**_AUTH, "Range": "bytes=0-99"},
        )
        body = await resp.read()

    assert resp.status == 206
    assert len(body) == 100


async def test_audio_is_cached_and_served_from_disk(tmp_path):
    client, audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 100,
    )
    async with client:
        first = await (await client.get("/segments/s1:0/audio", headers=_AUTH)).read()
        cached = audio.log_path("s1").parent / "seg" / "s1_0.ogg"
        assert cached.exists()
        second = await (await client.get("/segments/s1:0/audio", headers=_AUTH)).read()

    assert first == second


async def test_an_open_segment_is_served_live_and_not_cached(tmp_path):
    """It is still growing; a cached copy would be wrong within seconds."""
    client, audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 10, closed=False,
    )
    async with client:
        resp = await client.get("/segments/s1:0/audio", headers=_AUTH)
        await resp.read()

    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert not (audio.log_path("s1").parent / "seg").exists()


async def test_an_open_segment_supports_range_requests(tmp_path):
    """A segment still within the idle-close window (5 min) is open, but it
    must still be seekable — scrubbing a just-finished recording is the common
    case. Without Range the open path served a non-seekable 200, so
    MediaPlayer treated the source as unseekable and seekTo was a no-op: the
    'drags the bar but plays from where it started' bug.

    Range support is added by hand on the in-memory snapshot (open segments
    are never cached to disk, so FileResponse is not an option here).
    """
    client, audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 100, closed=False,
    )
    async with client:
        resp = await client.get(
            "/segments/s1:0/audio", headers={**_AUTH, "Range": "bytes=0-99"},
        )
        body = await resp.read()

    assert resp.status == 206
    assert len(body) == 100
    assert resp.headers["Content-Range"].startswith("bytes 0-99/")
    assert resp.headers["Accept-Ranges"] == "bytes"
    # Still never cached or written to disk — it's a live snapshot.
    assert resp.headers["Cache-Control"] == "no-store"
    assert not (audio.log_path("s1").parent / "seg").exists()


async def test_open_segment_without_range_advertises_accept_ranges(tmp_path):
    """The no-range 200 must advertise Accept-Ranges, or MediaPlayer marks the
    source unseekable up front and never even issues a Range request."""
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 10, closed=False,
    )
    async with client:
        resp = await client.get("/segments/s1:0/audio", headers=_AUTH)

    assert resp.status == 200
    assert resp.headers["Accept-Ranges"] == "bytes"


async def test_audio_for_a_session_with_no_log_is_404(tmp_path):
    client, _audio, _index = _client(tmp_path, events=[_event(0)])
    async with client:
        resp = await client.get("/segments/s1:0/audio", headers=_AUTH)

    assert resp.status == 404


async def test_audio_for_an_unknown_segment_is_404(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"],
    )
    async with client:
        resp = await client.get("/segments/s1:99/audio", headers=_AUTH)

    assert resp.status == 404


async def test_audio_requires_a_token(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"],
    )
    async with client:
        resp = await client.get("/segments/s1:0/audio")

    assert resp.status == 401


async def test_audio_covers_only_this_segments_window(tmp_path):
    """Two recordings in one session share one frame log, so the wrong slice
    would play the neighbouring conversation."""
    events = [
        _event(0, at=_T0),
        _event(1, at=_T0 + timedelta(minutes=30)),
    ]
    # Distinguishable frames, so a wrong window shows up as wrong bytes
    # rather than as a coincidentally identical stream.
    frames = [bytes([1 + i % 250, i % 256]) for i in range(200)]
    client, _audio, _index = _client(tmp_path, events=events, frames=frames)
    async with client:
        first = await (await client.get("/segments/s1:0/audio", headers=_AUTH)).read()
        second = await (await client.get("/segments/s1:1/audio", headers=_AUTH)).read()

    assert first != second


# ---- /waveform --------------------------------------------------------------


async def test_waveform_returns_bucketed_peaks(tmp_path):
    # 50 slots == 1000 ms == two 500 ms buckets.
    peaks = [255] * 25 + [51] * 25
    client, _audio, _index = _client(
        tmp_path, events=[_event(0, ms=1000)], frames=[b"aa"] * 50, peaks=peaks,
    )
    async with client:
        resp = await client.get("/segments/s1:0/waveform", headers=_AUTH)
        body = await resp.json()

    assert resp.status == 200
    assert body["schemaVersion"] == "v1"
    assert body["bucketMs"] == 500
    assert body["durationMs"] == 1000
    assert body["peaks"] == [1.0, 0.2]


async def test_waveform_peaks_are_normalised_to_zero_one(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 25, peaks=[128] * 25,
    )
    async with client:
        body = await (await client.get("/segments/s1:0/waveform", headers=_AUTH)).json()

    assert all(0.0 <= p <= 1.0 for p in body["peaks"])


async def test_waveform_silence_reads_as_zero(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 25, peaks=[0] * 25,
    )
    async with client:
        body = await (await client.get("/segments/s1:0/waveform", headers=_AUTH)).json()

    # The event is 1000 ms, so the window is two buckets; the second is the
    # zero-padded tail past the frames actually written.
    assert body["peaks"] == [0.0, 0.0]


async def test_waveform_with_no_audio_is_404(tmp_path):
    client, _audio, _index = _client(tmp_path, events=[_event(0)])
    async with client:
        resp = await client.get("/segments/s1:0/waveform", headers=_AUTH)

    assert resp.status == 404


async def test_waveform_for_an_unknown_segment_is_404(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"],
    )
    async with client:
        resp = await client.get("/segments/s1:99/waveform", headers=_AUTH)

    assert resp.status == 404


async def test_waveform_requires_a_token(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"],
    )
    async with client:
        resp = await client.get("/segments/s1:0/waveform")

    assert resp.status == 401


# ---- the codec fields stop lying (§3.2) -------------------------------------


async def test_event_wire_reports_the_real_codec_when_audio_exists(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 10,
    )
    async with client:
        body = await (await client.get("/segments/s1:0/events", headers=_AUTH)).json()

    event = body["events"][0]
    assert event["codec"] == "opus"
    assert event["sampleRateHz"] == 16000
    assert event["byteCount"] > 0


async def test_event_wire_reports_no_codec_when_there_is_no_audio(tmp_path):
    """A client can now tell "no audio" from "unknown format" — before the
    audio plane existed, both looked identical."""
    client, _audio, _index = _client(tmp_path, events=[_event(0)])
    async with client:
        body = await (await client.get("/segments/s1:0/events", headers=_AUTH)).json()

    event = body["events"][0]
    assert event["codec"] == ""
    assert event["sampleRateHz"] == 0
    assert event["byteCount"] == 0


async def test_segment_row_reports_has_audio(tmp_path):
    client, _audio, _index = _client(
        tmp_path, events=[_event(0)], frames=[b"aa"] * 10,
    )
    async with client:
        body = await (await client.get("/segments", headers=_AUTH)).json()

    assert body["segments"][0]["hasAudio"] is True
