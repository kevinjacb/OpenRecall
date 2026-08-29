"""Tests for the server-side PCM denoise layer.

The denoiser sits on the decode path of :class:`AudioIngestPipeline`, so it
sees the same PCM the transcriber and the speaker embedder see. It is off by
default (``NoopDenoiser``); ``NoisereduceDenoiser`` self-calibrates a noise
profile from the quietest hops and gates each subsequent hop against it.

These tests exercise the *logic* — passthrough, profile accumulation,
profile-commit on a quiet window, halving on a loud window, and the wired
default — with an injectable ``reduce_fn`` so they run without noisereduce
installed. A real-noisereduce round trip is guarded by ``importorskip``.
"""
from __future__ import annotations

import struct

from openrecall_server.ingest.audio_packet import AudioPacket, PacketType, VadState
from openrecall_server.ingest.denoise import (
    NoisereduceDenoiser,
    NoopDenoiser,
    build_denoiser,
)


def _pcm(samples: list[int]) -> bytes:
    """int16 mono PCM from a list of int16 sample values."""
    return struct.pack("<" + "h" * len(samples), *samples)


# ---- NoopDenoiser ------------------------------------------------------------


def test_noop_is_passthrough_and_reset_is_noop():
    d = NoopDenoiser()
    payload = _pcm([0, 100, -200, 32767])
    assert d.process(payload) is payload
    d.reset()  # must not raise
    assert d.process(b"") == b""


def test_build_denoiser_returns_noop_when_disabled():
    d = build_denoiser(enabled=False, sample_rate=16000)
    assert isinstance(d, NoopDenoiser)


def test_build_denoiser_returns_noisereduce_when_enabled():
    d = build_denoiser(enabled=True, sample_rate=16000)
    assert isinstance(d, NoisereduceDenoiser)


# ---- NoisereduceDenoiser: profile accumulation logic -------------------------


def test_passthrough_until_profile_ready_on_quiet_window():
    """While the profile is being built, process() is a pass-through."""
    d = NoisereduceDenoiser(16000, profile_ms=100, ambience_rms=300.0)
    # 100 ms at 16 kHz == 1600 samples == 3200 bytes. Feed two 50 ms chunks;
    # neither alone reaches the target, so both pass through untouched.
    quiet = _pcm([10] * 800)  # rms 10 << 300 -> genuinely quiet
    first = d.process(quiet)
    assert first == quiet  # still accumulating
    # RMS of 50 ms is low; but the target isn't met, so still passthrough.
    assert d._profile is None


def test_quiet_window_commits_profile_and_switches_to_denoise():
    """A quiet enough accumulation becomes the profile; later chunks are gated."""
    calls: list = []

    def fake_reduce(y, sr, *, y_noise, stationary=False):
        calls.append((int(sr), len(y_noise), stationary))
        return y  # identity — we only assert the gating path is taken

    d = NoisereduceDenoiser(
        16000, profile_ms=100, ambience_rms=300.0, reduce_fn=fake_reduce,
    )
    # One 100 ms chunk of quiet audio -> reaches target and is below ambience.
    quiet = _pcm([5] * 1600)
    assert d.process(quiet) == quiet  # the chunk that commits is still passthrough
    assert d._profile is not None
    # Subsequent chunk is now gated (reduce_fn invoked).
    nxt = _pcm([1000] * 1600)
    out = d.process(nxt)
    assert calls == [(16000, 1600, True)]
    # fake_reduce returns y unchanged, so output equals input.
    assert out == nxt


def test_loud_accumulation_is_halved_not_committed():
    """Speech dominating the accumulation must not become the noise profile."""
    d = NoisereduceDenoiser(
        16000, profile_ms=100, ambience_rms=300.0, reduce_fn=lambda y, sr, *, y_noise, stationary=False: y,
    )
    loud = _pcm([20000] * 1600)  # rms ~20000, far above ambience
    out = d.process(loud)
    assert out == loud  # passthrough (still building profile)
    # Profile was NOT committed: the loud window is not noise.
    assert d._profile is None
    # Half the accumulation was dropped, so the buffer shrank.
    assert len(d._acc) == len(loud) // 2


def test_best_effort_profile_after_max_wait():
    """If no quiet window ever appears, a best-effort profile is used rather
    than never denoising."""
    d = NoisereduceDenoiser(
        16000, profile_ms=100, max_profile_wait_ms=200,
        ambience_rms=300.0, reduce_fn=lambda y, sr, *, y_noise, stationary=False: y,
    )
    # target = 100 ms (1600 samples/3200 bytes), max = 200 ms (6400 bytes).
    # Feed loud audio in 100 ms chunks repeatedly. Each chunk reaches target,
    # is loud (so not committed as a quiet profile), is halved, and the
    # accumulator grows toward the max-wait ceiling. Once it crosses max, the
    # best-effort path commits whatever has accumulated.
    loud_chunk = _pcm([20000] * 1600)  # 100 ms
    for _ in range(10):
        if d._profile is not None:
            break
        d.process(loud_chunk)
    # Best-effort profile is now committed despite never seeing quiet audio.
    assert d._profile is not None


def test_reset_drops_profile_and_restarts_accumulation():
    d = NoisereduceDenoiser(
        16000, profile_ms=100, ambience_rms=300.0, reduce_fn=lambda y, sr, *, y_noise, stationary=False: y,
    )
    d.process(_pcm([5] * 1600))
    assert d._profile is not None
    d.reset()
    assert d._profile is None
    assert len(d._acc) == 0
    # After reset, a fresh quiet window must re-commit.
    assert d.process(_pcm([5] * 1600)) == _pcm([5] * 1600)
    assert d._profile is not None


def test_empty_chunks_dont_crash():
    d = NoisereduceDenoiser(16000, profile_ms=100, reduce_fn=lambda y, sr, *, y_noise, stationary=False: y)
    assert d.process(b"") == b""
    assert d._profile is None


# ---- pipeline wiring: default is passthrough ---------------------------------


def _pkt(chunk_seq: int, n_frames: int) -> AudioPacket:
    import struct as _s

    frames = [b"\x00"] * n_frames
    header = _s.pack(
        "<BIIBBB",
        (1 << 4) | PacketType.MEMORY_CHUNK,
        chunk_seq, chunk_seq * 20, VadState.SPEECH, len(frames), 0,
    )
    body = b"".join(_s.pack("<B", len(f)) + f for f in frames)
    return AudioPacket.parse(header + body)


class _FakeDecoder:
    """Each 'opus' frame decodes to a fixed int16 PCM blob."""

    BYTES_PER_FRAME = 640  # 320 samples * 2 bytes == 20 ms at 16 kHz

    def decode(self, frame: bytes) -> bytes:
        return b"\x00\x01" * (self.BYTES_PER_FRAME // 2)


class _NoTranscriber:
    """A legacy str-returning transcriber that emits nothing (the pipeline wraps
    it via streaming_from_text). We only care that the pipeline runs and the
    denoiser is invoked."""

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return ""


def test_pipeline_default_denoiser_is_passthrough():
    """Without a denoiser argument the pipeline uses NoopDenoiser and behaves
    exactly as before — no regression on a default install."""
    from openrecall_server.ingest.pipeline import AudioIngestPipeline
    from openrecall_server.ingest.reassembler import SessionReassembler

    pipe = AudioIngestPipeline(
        reassembler=SessionReassembler(),
        decoder=_FakeDecoder(),
        transcriber=_NoTranscriber(),
        window_ms=2000,
        hop_ms=1000,
    )
    from openrecall_server.ingest.denoise import NoopDenoiser

    assert isinstance(pipe._denoiser, NoopDenoiser)
    # A full packet ingests without error.
    out = pipe.ingest(_pkt(0, 50))  # 50 frames == 1s
    assert out == []


class _RecordingDenoiser:
    """A test double that records every chunk it sees and returns it unchanged."""

    def __init__(self):
        self.seen: list[bytes] = []

    def process(self, pcm: bytes) -> bytes:
        self.seen.append(pcm)
        return pcm

    def reset(self) -> None:
        self.seen.clear()


def _make_denoise_pipe(denoiser):
    from openrecall_server.ingest.pipeline import AudioIngestPipeline
    from openrecall_server.ingest.reassembler import SessionReassembler

    return AudioIngestPipeline(
        reassembler=SessionReassembler(),
        decoder=_FakeDecoder(),
        transcriber=_NoTranscriber(),
        window_ms=2000,
        hop_ms=1000,
        denoiser=denoiser,
    )


def test_pipeline_invokes_wired_denoiser_per_hop():
    """A wired denoiser sees HOP-sized chunks, not 20 ms frames: the spectral
    gate needs an STFT window (~128 ms) of context, so 320-sample frame
    chunks would make it a no-op."""
    denoiser = _RecordingDenoiser()
    pipe = _make_denoise_pipe(denoiser)
    pipe.ingest(_pkt(0, 50))  # 50 frames == 1s == exactly one 1000 ms hop
    assert len(denoiser.seen) == 1
    assert len(denoiser.seen[0]) == 50 * _FakeDecoder.BYTES_PER_FRAME


def test_flush_denoises_the_partial_tail():
    """The sub-hop tail at session end goes through the denoiser too."""
    denoiser = _RecordingDenoiser()
    pipe = _make_denoise_pipe(denoiser)
    pipe.ingest(_pkt(0, 10))  # 200 ms — below the 1000 ms hop
    assert denoiser.seen == []
    pipe.flush()
    assert len(denoiser.seen) == 1
    assert len(denoiser.seen[0]) == 10 * _FakeDecoder.BYTES_PER_FRAME


def test_synthesized_gap_silence_bypasses_denoiser():
    """All-zero hops (synthesized VAD-gap silence) must not reach the
    denoiser — a zeros noise profile would make the gate a no-op."""
    import struct as _s

    def _pkt_at(chunk_seq, n_frames, rel_ts_ms):
        frames = [b"\x00"] * n_frames
        header = _s.pack(
            "<BIIBBB",
            (1 << 4) | PacketType.MEMORY_CHUNK,
            chunk_seq, rel_ts_ms, VadState.SPEECH, len(frames), 0,
        )
        body = b"".join(_s.pack("<B", len(f)) + f for f in frames)
        return AudioPacket.parse(header + body)

    denoiser = _RecordingDenoiser()
    pipe = _make_denoise_pipe(denoiser)
    # 1s speech at rel 0, then speech resuming at rel 2000 -> 1000 ms of
    # synthesized zeros (one full all-zero hop) between them.
    pipe.ingest(_pkt_at(0, 50, 0))
    pipe.ingest(_pkt_at(1, 50, 2000))
    pipe.flush()
    assert denoiser.seen, "real audio must still be denoised"
    for chunk in denoiser.seen:
        assert chunk.count(0) != len(chunk), "all-zero hop reached the denoiser"


# ---- real noisereduce (guarded) ---------------------------------------------


def test_real_noisereduce_round_trip_is_int16_pcm():
    """End-to-end with the real library: a noisy signal is denoised and the
    output is valid int16 PCM of the same length. Skipped when noisereduce or
    numpy is not installed."""
    pytest_importorskip = __import__("pytest")
    pytest_importorskip.importorskip("noisereduce")
    pytest_importorskip.importorskip("numpy")

    d = NoisereduceDenoiser(16000, profile_ms=100, ambience_rms=300.0)
    # Commit a quiet profile.
    d.process(_pcm([5] * 1600))
    assert d._profile is not None
    # Denoise a louder chunk: output must be int16 PCM of matching length.
    out = d.process(_pcm([1000] * 1600))
    assert len(out) == 3200  # 1600 samples * 2 bytes
    assert len(out) % 2 == 0