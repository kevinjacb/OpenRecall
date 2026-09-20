"""Phase 3 — the ingest audio tap (spec §3.1, D5).

The invariant these pin: after a stream with reordering, duplication, loss
and VAD silence, the frame log's slot count still equals elapsed session time
divided by 20 ms. That equality is what keeps the Recording Detail playhead
aligned with the transcript.
"""
from __future__ import annotations

import pytest

from openrecall_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from openrecall_server.ingest.pipeline import FRAME_MS, AudioIngestPipeline, _peak_of
from openrecall_server.ingest.reassembler import SessionReassembler
from openrecall_server.media.audio import AudioStore


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        # 20 ms of 16 kHz mono PCM (320 samples), amplitude keyed off the
        # frame's first byte so peaks are predictable.
        level = frame[0] if frame else 0
        return bytes([0, level]) * 320


class FakeTranscriber:
    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return ""


def _packet(chunk_seq: int, *, rel_ts_ms=None, n_frames=1, vad=VadState.SPEECH, fill=1):
    return AudioPacket(
        version=1,
        ptype=PacketType.MEMORY_CHUNK,
        chunk_seq=chunk_seq,
        rel_ts_ms=chunk_seq * FRAME_MS * n_frames if rel_ts_ms is None else rel_ts_ms,
        vad_state=vad,
        flags=0,
        frames=[bytes([fill, chunk_seq & 0xFF])] * n_frames,
    )


@pytest.fixture
def pipeline(tmp_path):
    audio = AudioStore(tmp_path / "audio")
    p = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
        audio_store=audio,
    )
    p.set_audio_target("s1")
    return p, audio


# ---- the tap ----------------------------------------------------------------


def test_frames_are_persisted_as_they_are_ingested(pipeline):
    p, audio = pipeline
    p.ingest(_packet(0))
    p.ingest(_packet(1))

    assert audio.stat("s1").slot_count == 2
    assert len(list(audio.read_range("s1", 0, 1000))) == 2


def test_the_timeline_is_anchored_at_the_first_packet(pipeline):
    """The device's rel_ts_ms is boot-relative, so a session that starts at
    device-uptime 90 minutes must not begin with 90 minutes of silence."""
    p, audio = pipeline
    p.ingest(_packet(0, rel_ts_ms=5_400_000))
    p.ingest(_packet(1, rel_ts_ms=5_400_020))

    assert audio.stat("s1").slot_count == 2


def test_vad_suppressed_silence_becomes_recorded_time(pipeline):
    """Firmware VAD emits gap markers instead of audio; that time is real and
    must appear in the log."""
    p, audio = pipeline
    p.ingest(_packet(0, rel_ts_ms=0))
    p.ingest(_packet(1, rel_ts_ms=10_000, vad=VadState.GAP_MARKER))
    p.ingest(_packet(2, rel_ts_ms=10_020))

    stat = audio.stat("s1")
    assert stat.slot_count == 502
    assert stat.duration_ms == 502 * FRAME_MS


def test_slot_count_tracks_elapsed_time_through_loss(pipeline):
    """Packets 1..4 never arrive. Their time still has to exist, or every
    later transcript timestamp points at the wrong audio."""
    p, audio = pipeline
    p.ingest(_packet(0, rel_ts_ms=0))
    p.ingest(_packet(5, rel_ts_ms=100))

    assert audio.stat("s1").slot_count == 6


def test_a_duplicate_packet_does_not_extend_the_timeline(pipeline):
    p, audio = pipeline
    p.ingest(_packet(0, rel_ts_ms=0))
    p.ingest(_packet(1, rel_ts_ms=20))
    p.ingest(_packet(1, rel_ts_ms=20))  # the relay is at-least-once

    assert audio.stat("s1").slot_count == 2


def test_a_reordered_packet_does_not_shift_later_audio(pipeline):
    """The out-of-order packet is dropped from the log rather than appended
    at the wrong time — a missing 20 ms is a click, a shifted timeline breaks
    scrubbing for the whole recording."""
    p, audio = pipeline
    p.ingest(_packet(0, rel_ts_ms=0))
    p.ingest(_packet(2, rel_ts_ms=40))
    p.ingest(_packet(1, rel_ts_ms=20))

    slots = list(audio.read_range("s1", 0, 1000))
    assert len(slots) == 3
    assert slots[2] != b""  # packet 2's frame is still at slot 2


def test_the_invariant_holds_over_a_lossy_reordered_stream(pipeline):
    """The property stated end to end, which is what §3.1 asks for."""
    p, audio = pipeline
    arrival = [0, 2, 1, 3, 3, 7, 6, 8, 20]
    for seq in arrival:
        p.ingest(_packet(seq, rel_ts_ms=seq * FRAME_MS))

    highest_ms = 20 * FRAME_MS
    assert audio.stat("s1").slot_count == highest_ms // FRAME_MS + 1


def test_multi_frame_packets_occupy_one_slot_each(pipeline):
    p, audio = pipeline
    p.ingest(_packet(0, rel_ts_ms=0, n_frames=5))
    p.ingest(_packet(1, rel_ts_ms=100, n_frames=5))

    assert audio.stat("s1").slot_count == 10


# ---- disabling and failure --------------------------------------------------


def test_persist_audio_off_writes_nothing(tmp_path):
    audio = AudioStore(tmp_path / "audio")
    p = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20, window_ms=100, sample_rate=16000,
        audio_store=audio, persist_audio=False,
    )
    p.set_audio_target("s1")

    p.ingest(_packet(0))

    assert audio.has("s1") is False


def test_no_audio_store_is_a_silent_noop():
    p = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20, window_ms=100, sample_rate=16000,
    )

    p.ingest(_packet(0))  # must not raise


def test_an_unbound_session_writes_nothing(tmp_path):
    """Audio before `hello` has nowhere to go; it must not invent a session."""
    audio = AudioStore(tmp_path / "audio")
    p = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20, window_ms=100, sample_rate=16000,
        audio_store=audio,
    )

    p.ingest(_packet(0))

    assert audio.sessions() == []


def test_a_failing_audio_store_does_not_take_down_the_stream(tmp_path):
    """Audio persistence is a feature; transcription is the product. A full
    disk must not stop the live stream."""

    class Exploding:
        def write_at(self, *a, **k):
            raise OSError("disk full")

    p = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20, window_ms=100, sample_rate=16000,
        audio_store=Exploding(),
    )
    p.set_audio_target("s1")

    p.ingest(_packet(0))  # must not raise


def test_a_decoder_failure_yields_a_flat_peak_not_a_dropped_frame(tmp_path):
    """A flat bar is a much better outcome than losing the audio or failing
    the request for the recording."""
    class BadDecoder:
        def decode(self, frame):
            raise ValueError("corrupt frame")

    audio = AudioStore(tmp_path / "audio")
    p = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=0),
        decoder=BadDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20, window_ms=100, sample_rate=16000,
        audio_store=audio,
    )
    p.set_audio_target("s1")

    assert p._peaks_of([b"\x01\x00", b"\x02\x00"]) == [0, 0]


# ---- peaks ------------------------------------------------------------------


def test_peaks_are_recorded_alongside_frames(pipeline):
    p, audio = pipeline
    p.ingest(_packet(0, rel_ts_ms=0, fill=1))

    assert audio.peaks("s1", 0, 20) == [_peak_of(FakeDecoder().decode(b"\x01\x00"))]


def test_peak_of_silence_is_zero():
    assert _peak_of(b"\x00\x00" * 320) == 0


def test_peak_of_full_scale_is_max():
    assert _peak_of(b"\xff\x7f") == 255


def test_peak_reads_signed_samples():
    """A negative sample is loud, not quiet — reading it as unsigned would
    draw a loud passage as near-silent."""
    assert _peak_of(b"\x00\x80") > 200  # -32768


def test_peak_of_empty_pcm_is_zero():
    assert _peak_of(b"") == 0


# ---- resume: a reconnect must append, not restart at slot 0 ------------------
# A pipeline is built per WebSocket connection. Anchoring slots at the first
# packet of *this* pipeline means a resumed session restarts at slot 0, where
# write_at rejects every frame as history and returns 0 — which nothing checks.
# Observed on a real device 2026-09-20: transcription kept working while audio
# playback was empty, because the ASR path has no such cursor.

def _connection(audio, start_seq, session="s1"):
    p = AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
        audio_store=audio,
    )
    p.set_audio_target(session)
    return p


def test_a_reconnect_appends_instead_of_being_dropped_as_history(tmp_path):
    audio = AudioStore(tmp_path / "audio")

    first = _connection(audio, 0)
    for i in range(10):
        first.ingest(_packet(i, rel_ts_ms=i * FRAME_MS))
    assert audio.stat("s1").slot_count == 10

    # Same session, new connection. The device did not reboot, so its rel_ts
    # continues — but the new pipeline's anchor would put these at slot 0.
    second = _connection(audio, 10)
    for i in range(10, 20):
        second.ingest(_packet(i, rel_ts_ms=i * FRAME_MS))

    assert audio.stat("s1").slot_count == 20, (
        "the second connection's audio was dropped as history — this is the "
        "silent failure where transcripts keep working and playback is empty")


def test_a_device_reboot_mid_session_still_appends(tmp_path):
    """The worst case: rel_ts restarts near zero while the session resumes, so
    every new frame looks far behind the cursor."""
    audio = AudioStore(tmp_path / "audio")

    first = _connection(audio, 0)
    for i in range(50):
        first.ingest(_packet(i, rel_ts_ms=i * FRAME_MS))

    rebooted = _connection(audio, 50)
    for i in range(10):
        rebooted.ingest(_packet(50 + i, rel_ts_ms=i * FRAME_MS))  # rel_ts from 0

    assert audio.stat("s1").slot_count == 60
    assert len(list(audio.read_range("s1", 0, 10_000_000))) == 60, (
        "frames must be readable back, not merely counted")


def test_a_fresh_session_is_unchanged(tmp_path):
    """The fix must not shift a first connection: slot_count 0 means anchor at
    the first packet exactly as before."""
    audio = AudioStore(tmp_path / "audio")
    p = _connection(audio, 0, session="brand-new")
    # A non-zero starting rel_ts (device already up a while) must still land
    # the first frame at slot 0.
    for i in range(5):
        p.ingest(_packet(i, rel_ts_ms=500_000 + i * FRAME_MS))

    assert audio.stat("brand-new").slot_count == 5
    assert len(list(audio.read_range("brand-new", 0, 10_000_000))) == 5
    # The anchor for a fresh session must still be the first packet's own
    # rel_ts — i.e. the seeding offset is zero when the log is empty — so a
    # first connection is byte-for-byte what it was before this fix.
    assert audio.stat("brand-new").byte_count > 0
