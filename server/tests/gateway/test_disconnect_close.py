"""Phase 0 — the disconnect path closes the session (spec §0.1, §0.2, §0.3).

`bye` is unreliable: on a WebSocket drop the relay writes it into an
already-dead socket, so the server usually never sees one. Before this
slice, `deregister` and `mark_closed` were reachable *only* from `_on_bye`,
so an abrupt disconnect leaked the session id into `SessionLifecycle._active`
forever (inflating `activeSessions`) and left `ended_at` permanently null.

These tests pin `on_disconnect` as the close path that always fires.
"""
from __future__ import annotations

from datetime import datetime, timezone

from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.gateway.core import GatewayCore
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler
from openrecall_server.protocol.messages import Bye, Hello
from openrecall_server.sessions.index import SessionIndex
from openrecall_server.sessions.lifecycle import SessionLifecycle

from .test_core_index_hook import FakeDecoder, FakeTranscriber, audio_bytes

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_core() -> tuple[GatewayCore, InMemoryEventStore, SessionIndex, SessionLifecycle]:
    store = InMemoryEventStore()
    index = SessionIndex(clock=lambda: _NOW)
    lifecycle = SessionLifecycle()

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=100,
            sample_rate=16000,
        )

    core = GatewayCore(
        pipeline_factory=factory,
        event_store=store,
        session_index=index,
        session_lifecycle=lifecycle,
    )
    return core, store, index, lifecycle


# ---- §0.1 lifecycle leak ----------------------------------------------------


def test_abrupt_disconnect_deregisters_the_session():
    core, _store, _index, lifecycle = _make_core()
    core.on_control(Hello(session_id="s1", start_seq=0))
    assert lifecycle.is_active("s1")

    core.on_disconnect()  # no bye — the socket just died

    assert not lifecycle.is_active("s1")
    assert len(lifecycle) == 0


def test_on_disconnect_after_bye_is_a_noop():
    core, _store, _index, lifecycle = _make_core()
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_control(Bye(session_id="s1"))
    assert len(lifecycle) == 0

    core.on_disconnect()  # both paths run; the second must not raise

    assert len(lifecycle) == 0


def test_on_disconnect_without_hello_is_a_noop():
    core, _store, _index, lifecycle = _make_core()
    core.on_disconnect()
    assert len(lifecycle) == 0


# ---- §0.2 sessions never end ------------------------------------------------


def test_bye_stamps_ended_at():
    core, _store, index, _lifecycle = _make_core()
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=5))
    assert index.summary("s1").ended_at is None

    core.on_control(Bye(session_id="s1"))

    assert index.summary("s1").ended_at is not None


def test_abrupt_disconnect_stamps_ended_at():
    core, _store, index, _lifecycle = _make_core()
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=5))

    core.on_disconnect()

    summary = index.summary("s1")
    assert summary.ended_at is not None
    # duration_ms is now bounded by ended_at instead of racing `now` forever.
    assert summary.duration_ms() == summary.duration_ms(now=_NOW)


def test_a_reconnect_reopens_the_closed_session():
    """The relay reuses one session id across reconnects (spec D1), so a new
    event must clear `ended_at` rather than leave it pinned to the first drop.
    """
    core, _store, index, _lifecycle = _make_core()
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=5))
    core.on_disconnect()
    assert index.summary("s1").ended_at is not None

    core.on_control(Hello(session_id="s1", start_seq=5))
    core.on_audio(audio_bytes(5, n_frames=5))

    assert index.summary("s1").ended_at is None


def test_mark_closed_on_unknown_session_is_a_noop():
    index = SessionIndex(clock=lambda: _NOW)
    index.mark_closed("nope", _NOW)  # must not raise or create a summary
    assert index.summary("nope") is None
