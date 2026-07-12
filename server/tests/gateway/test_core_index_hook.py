"""Tests for the SessionIndex/SessionLifecycle hook in `GatewayCore`.

`GatewayCore` gains two optional constructor kwargs:

* `session_index` — fed by every successfully-stored capture event.
* `session_lifecycle` — registered on `_on_hello`, deregistered on `_on_bye`.

Both default to `None` so the existing tests (which construct a bare
`GatewayCore`) keep working unchanged.
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

from sense_server.events.store import InMemoryEventStore
from sense_server.gateway.core import GatewayCore
from sense_server.ingest.audio_packet import PacketType, VadState
from sense_server.ingest.pipeline import AudioIngestPipeline
from sense_server.ingest.reassembler import SessionReassembler
from sense_server.protocol.messages import Bye, Hello
from sense_server.sessions.index import SessionIndex
from sense_server.sessions.lifecycle import SessionLifecycle


# ---- fixtures / fakes -------------------------------------------------------


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls += 1
        return f"seg{self.calls}"


def audio_bytes(chunk_seq: int, n_frames: int) -> bytes:
    frames = [bytes([chunk_seq & 0xFF])] * n_frames
    header = struct.pack(
        "<BIIBBB",
        (1 << 4) | PacketType.MEMORY_CHUNK,
        chunk_seq,
        chunk_seq * 20,
        VadState.SPEECH,
        len(frames),
        0,
    )
    return header + b"".join(struct.pack("<B", len(f)) + f for f in frames)


def make_core_with_index(
    window_ms: int = 100,
) -> tuple[GatewayCore, InMemoryEventStore, SessionIndex]:
    store = InMemoryEventStore()
    index = SessionIndex(clock=lambda: datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc))

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=window_ms,
            sample_rate=16000,
        )

    core = GatewayCore(pipeline_factory=factory, event_store=store, session_index=index)
    return core, store, index


def make_core_with_lifecycle() -> tuple[GatewayCore, SessionLifecycle]:
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

    core = GatewayCore(pipeline_factory=factory, session_lifecycle=lifecycle)
    return core, lifecycle


# ---- index hook -------------------------------------------------------------


def test_core_with_index_records_newly_stored_events():
    """5 frames @ hop=20ms = 5 streaming transcripts, each persisted as one event."""
    core, store, index = make_core_with_index(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))

    core.on_audio(audio_bytes(0, n_frames=5))

    s = index.summary("s1")
    assert s is not None
    assert s.event_count == 5
    assert s.transcript_count == 5


def test_core_with_index_does_not_record_duplicates():
    """A replay that the store dedupes (event_id collision) must NOT bump the index.

    Under streaming, the first pass records 5 events (one per hop).
    The second pass replays the same 5 events; the store dedupes by
    event_id, so the replay is a no-op. The index records only the
    first pass.
    """
    core, store, index = make_core_with_index(window_ms=100)
    for _ in range(2):  # same session captured twice
        core.on_control(Hello(session_id="s1", start_seq=0))
        core.on_audio(audio_bytes(0, n_frames=5))
        core.on_control(Bye(session_id="s1"))

    # First pass: 5 events. Second pass: 0 (all event_ids collide; store
    # dedupes). The replay is a no-op for the store.
    assert len(store.events("s1")) == 5
    assert index.summary("s1").event_count == 5


def test_core_without_index_works_as_before():
    """Back-compat: a core constructed without session_index still stores events."""
    store = InMemoryEventStore()

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=100,
            sample_rate=16000,
        )

    core = GatewayCore(pipeline_factory=factory, event_store=store)  # no session_index
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=5))
    core.on_control(Bye(session_id="s1"))

    # 5 frames @ hop=20ms = 5 streaming transcripts, each persisted.
    assert len(store.events("s1")) == 5


# ---- lifecycle hook ---------------------------------------------------------


def test_lifecycle_registers_on_hello():
    core, lifecycle = make_core_with_lifecycle()
    assert len(lifecycle) == 0

    core.on_control(Hello(session_id="s1", start_seq=0))

    assert len(lifecycle) == 1
    assert lifecycle.is_active("s1")


def test_lifecycle_deregisters_on_bye():
    core, lifecycle = make_core_with_lifecycle()
    core.on_control(Hello(session_id="s1", start_seq=0))
    assert len(lifecycle) == 1

    core.on_control(Bye(session_id="s1"))

    assert len(lifecycle) == 0
    assert not lifecycle.is_active("s1")


def test_lifecycle_without_kwarg_works_as_before():
    """A core with no session_lifecycle still serves Hello/Bye without raising."""

    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=100,
            sample_rate=16000,
        )

    core = GatewayCore(pipeline_factory=factory)  # no session_lifecycle
    out = core.on_control(Hello(session_id="s1", start_seq=0))
    assert out  # ack at minimum
    # No exception on Bye is the load-bearing assertion; it returns a list.
    out = core.on_control(Bye(session_id="s1"))
    assert out == []  # no buffered audio -> no transcripts on flush


def test_lifecycle_count_reflects_open_connections_in_isolation():
    """Two cores in the same lifecycle registry track independently."""
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

    core1 = GatewayCore(pipeline_factory=factory, session_lifecycle=lifecycle)
    core2 = GatewayCore(pipeline_factory=factory, session_lifecycle=lifecycle)
    core1.on_control(Hello(session_id="s1", start_seq=0))
    core2.on_control(Hello(session_id="s2", start_seq=0))
    assert len(lifecycle) == 2
    core1.on_control(Bye(session_id="s1"))
    assert len(lifecycle) == 1
    assert lifecycle.is_active("s2")
    assert not lifecycle.is_active("s1")
    core2.on_control(Bye(session_id="s2"))
    assert len(lifecycle) == 0
