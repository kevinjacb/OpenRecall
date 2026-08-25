import struct
from datetime import datetime, timedelta, timezone

from openrecall_server.commands.dispatcher import CommandDispatcher
from openrecall_server.commands.model import Command
from openrecall_server.commands.signing import CommandSigner
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.gateway.core import GatewayCore
from openrecall_server.ingest.audio_packet import PacketType, VadState
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler
from openrecall_server.protocol.messages import Ack, CommandAck, Hello, TranscriptMsg

NOW = datetime.now(timezone.utc)


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls += 1
        return f"seg{self.calls}"


def factory(start_seq: int) -> AudioIngestPipeline:
    return AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
    )


def audio_bytes(chunk_seq: int, n_frames: int, vad: int = VadState.SPEECH) -> bytes:
    frames = [bytes([chunk_seq & 0xFF])] * n_frames
    header = struct.pack(
        "<BIIBBB",
        (1 << 4) | PacketType.MEMORY_CHUNK,
        chunk_seq,
        chunk_seq * 20,
        vad,
        len(frames),
        0,
    )
    return header + b"".join(struct.pack("<B", len(f)) + f for f in frames)


class SpyDetector:
    """Records feed() calls; carries a _provenance map like the real detector."""
    def __init__(self):
        self.fed = []
        self._provenance = {}

    def feed(self, session_id, event):
        self.fed.append((session_id, event.event_id, event.text))


class SpyMemoryWriter:
    def __init__(self):
        self.acks = []

    def on_ack(self, command_id, session_id, command_type, source_event_id, trigger_text):
        self.acks.append((command_id, session_id, command_type, source_event_id))


def a_command(command_id: str, session_id: str = "s1") -> Command:
    return Command(
        command_id=command_id, session_id=session_id, type="capture_photo",
        params={}, issued_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )


def test_emit_feeds_detector_on_each_stored_event():
    detector = SpyDetector()
    core = GatewayCore(
        pipeline_factory=factory,
        event_store=InMemoryEventStore(),
        command_detector=detector,
    )
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=5))  # 5 hops -> 5 transcript events

    assert len(detector.fed) == 5
    assert detector.fed[0] == ("s1", "s1:0", "seg1")  # session_id, event_id, text


def test_command_ack_writes_memory_for_provenanced_command():
    detector = SpyDetector()
    memory = SpyMemoryWriter()
    dispatcher = CommandDispatcher(CommandSigner.generate())
    core = GatewayCore(
        pipeline_factory=factory, dispatcher=dispatcher,
        command_detector=detector, command_memory_writer=memory,
    )
    core.on_control(Hello(session_id="s1", start_seq=0))
    dispatcher.issue(a_command("c1"))
    # Simulate the detector having recorded that c1 came from transcript event s1:3.
    detector._provenance["c1"] = "s1:3"

    core.on_control(CommandAck(session_id="s1", command_id="c1"))

    assert memory.acks == [("c1", "s1", "capture_photo", "s1:3")]


def test_command_ack_without_provenance_does_not_write_memory():
    detector = SpyDetector()
    memory = SpyMemoryWriter()
    dispatcher = CommandDispatcher(CommandSigner.generate())
    core = GatewayCore(
        pipeline_factory=factory, dispatcher=dispatcher,
        command_detector=detector, command_memory_writer=memory,
    )
    core.on_control(Hello(session_id="s1", start_seq=0))
    dispatcher.issue(a_command("c2"))
    # No provenance entry for c2 (e.g. an HTTP /agent-issued command) -> no memory.
    core.on_control(CommandAck(session_id="s1", command_id="c2"))
    assert memory.acks == []


def test_emit_with_no_detector_is_a_noop_regression_guard():
    core = GatewayCore(pipeline_factory=factory, event_store=InMemoryEventStore())
    core.on_control(Hello(session_id="s1", start_seq=0))
    out = core.on_audio(audio_bytes(0, n_frames=5))  # must not raise
    # Transcripts still emitted exactly as before the wiring (5 + ack).
    assert len([m for m in out if isinstance(m, TranscriptMsg)]) == 5
    assert [m for m in out if isinstance(m, Ack)] == [Ack(session_id="s1", next_seq=1)]
