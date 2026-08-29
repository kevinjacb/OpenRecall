"""Tests for :class:`UtteranceTranscriber` (utterance-mode transcription).

Real-device evidence (2026-08-29) showed the rolling-window streaming path
re-emitting overlapping Parakeet output ("trans transcription not not
correct?") because TDT token timestamps shift between overlapping calls,
while a single batch call over the same audio was near-perfect. Utterance
mode transcribes each utterance exactly once, so these tests pin the
no-retranscription contract: the backend sees each byte of audio at most
once, and emission happens on end-of-utterance silence, the size cap, or
flush.
"""
from openrecall_server.ingest.streaming_transcriber import Token
from openrecall_server.ingest.utterance_transcriber import UtteranceTranscriber

SR = 16000
BYTES_PER_MS = SR * 2 // 1000


def _hop(ms: int, fill: bytes = b"\x64\x00") -> bytes:
    """``ms`` of audible PCM (constant sample 100 > epsilon)."""
    return fill * (ms * BYTES_PER_MS // 2)


def _zeros(ms: int) -> bytes:
    return b"\x00" * (ms * BYTES_PER_MS)


class ScriptedBackend:
    """Records every transcribe() call; returns the scripted tokens."""

    def __init__(self, tokens=None):
        self.calls: list[bytes] = []
        self._tokens = tokens or []

    def transcribe(self, pcm: bytes, sample_rate: int):
        assert sample_rate == SR
        self.calls.append(pcm)
        return list(self._tokens)


def test_no_backend_call_until_end_silence():
    b = ScriptedBackend()
    t = UtteranceTranscriber(b, end_silence_ms=560)
    for _ in range(10):
        assert t.feed(_hop(240)) == []
    assert b.calls == []


def test_end_silence_closes_utterance_with_one_call():
    b = ScriptedBackend([Token("Hello,", 100, 400, 1), Token(" world.", 450, 900, 1)])
    t = UtteranceTranscriber(b, end_silence_ms=560)
    t.feed(_hop(1000))
    out = []
    out += t.feed(_zeros(240))   # trailing zeros 240 < 560
    out += t.feed(_zeros(240))   # 480 < 560
    assert out == [] and b.calls == []
    out = t.feed(_zeros(240))    # 720 >= 560 -> close
    assert len(b.calls) == 1
    assert len(b.calls[0]) == 1720 * BYTES_PER_MS  # the whole buffered utterance
    assert [s.text for s in out] == ["Hello, world."]


def test_each_byte_transcribed_at_most_once():
    """The no-retranscription contract: consecutive utterances hand the
    backend disjoint audio."""
    b = ScriptedBackend([Token("a", 0, 100, 1)])
    t = UtteranceTranscriber(b, end_silence_ms=560)
    t.feed(_hop(500))
    t.feed(_zeros(600))          # close utterance 1
    t.feed(_hop(500, b"\x32\x00"))
    t.feed(_zeros(600))          # close utterance 2
    assert len(b.calls) == 2
    total = sum(len(c) for c in b.calls)
    assert total == (500 + 600 + 500 + 600) * BYTES_PER_MS  # no overlap


def test_one_segment_per_backend_sentence():
    b = ScriptedBackend([
        Token("One.", 0, 300, 1),
        Token("Two", 400, 600, 2), Token(" three.", 650, 900, 2),
    ])
    t = UtteranceTranscriber(b, end_silence_ms=560)
    t.feed(_hop(1000))
    out = t.feed(_zeros(600))
    assert [s.text for s in out] == ["One.", "Two three."]
    assert out[0].sentence_id == 1 and out[1].sentence_id == 2


def test_segments_carry_absolute_session_timestamps():
    b = ScriptedBackend([Token("hi", 100, 400, 1)])
    t = UtteranceTranscriber(b, end_silence_ms=560)
    # First utterance closes at 1100 ms of session time; second starts there.
    t.feed(_hop(500))
    t.feed(_zeros(600))
    b._tokens = [Token("again", 200, 500, 1)]
    t.feed(_hop(500))
    out = t.feed(_zeros(600))
    assert out[0].start_ms == 1100 + 200
    assert out[0].end_ms == 1100 + 500


def test_silence_only_buffer_is_not_transcribed():
    b = ScriptedBackend()
    t = UtteranceTranscriber(b, end_silence_ms=560, min_speech_ms=240)
    t.feed(_zeros(240))
    t.feed(_hop(100))            # 100 ms audible < 240 ms min
    out = t.feed(_zeros(600))
    assert out == [] and b.calls == []
    assert t.flush() == [] and b.calls == []


def test_max_utterance_cap_forces_transcription():
    b = ScriptedBackend([Token("long", 0, 100, 1)])
    t = UtteranceTranscriber(b, end_silence_ms=560, max_utterance_ms=2000)
    out = []
    for _ in range(10):          # 2400 ms of speech, no pause
        out += t.feed(_hop(240))
    assert len(b.calls) == 1     # cap fired once at >= 2000 ms
    assert [s.text for s in out] == ["long"]


def test_flush_transcribes_the_remainder():
    b = ScriptedBackend([Token("tail", 0, 300, 1)])
    t = UtteranceTranscriber(b, end_silence_ms=560)
    t.feed(_hop(700))
    out = t.flush()
    assert len(b.calls) == 1
    assert [s.text for s in out] == ["tail"]
    assert t.flush() == [] and len(b.calls) == 1  # nothing left


def test_audio_after_zeros_resets_the_silence_run():
    b = ScriptedBackend([Token("x", 0, 100, 1)])
    t = UtteranceTranscriber(b, end_silence_ms=560)
    t.feed(_hop(240))
    t.feed(_zeros(400))          # a mid-thought pause, below the threshold
    t.feed(_hop(240))            # speech resumes -> run resets
    assert b.calls == []
    t.feed(_zeros(400))
    assert b.calls == []         # 400 < 560: still one open utterance
    t.feed(_zeros(240))          # 640 >= 560 -> close
    assert len(b.calls) == 1


def test_duck_type_surface():
    t = UtteranceTranscriber(ScriptedBackend(), hop_ms=240)
    assert t._hop_ms == 240
    assert t.committed_ms == 0
    t.feed(_hop(500))
    t.feed(_zeros(600))
    assert t.committed_ms == 1100  # everything emitted so far


# ---------------------------------------------------------------------------
# Utterance-level speaker fingerprinting (pipeline integration)
# ---------------------------------------------------------------------------
def test_last_utterance_pcm_exposed_on_close():
    b = ScriptedBackend([Token("hi", 0, 100, 1)])
    t = UtteranceTranscriber(b, end_silence_ms=560)
    assert t.last_utterance_pcm is None
    t.feed(_hop(500))
    t.feed(_zeros(600))
    assert t.last_utterance_pcm is not None
    assert len(t.last_utterance_pcm) == (500 + 600) * BYTES_PER_MS


def test_pipeline_fingerprints_once_per_utterance_from_utterance_audio():
    """Utterance mode: the identifier runs ONCE per closed utterance on the
    utterance's own PCM with the synthesized gap fill stripped — never on
    per-hop rolling windows."""
    import struct
    from openrecall_server.ingest.audio_packet import AudioPacket, PacketType, VadState
    from openrecall_server.ingest.pipeline import AudioIngestPipeline
    from openrecall_server.ingest.reassembler import SessionReassembler

    class Decoder:
        def decode(self, frame):
            return b"\x64\x00" * 320  # audible constant

    class Identifier:
        def __init__(self):
            self.calls = []

        def identify(self, pcm, sr):
            self.calls.append(pcm)
            from openrecall_server.ingest.speaker_identifier import SpeakerAssignment
            return SpeakerAssignment("spk-1", 0.9, "confirmed")

    def pkt(seq, n_frames, rel_ts, vad=VadState.SPEECH):
        frames = [b"\x01"] * n_frames
        header = struct.pack(
            "<BIIBBB", (1 << 4) | PacketType.MEMORY_CHUNK, seq, rel_ts,
            vad, len(frames), 0)
        body = b"".join(struct.pack("<B", len(f)) + f for f in frames)
        return AudioPacket.parse(header + body)

    backend = ScriptedBackend([Token("hello", 0, 400, 1)])
    ident = Identifier()
    pipe = AudioIngestPipeline(
        reassembler=SessionReassembler(),
        decoder=Decoder(),
        transcriber=UtteranceTranscriber(backend, end_silence_ms=560),
        hop_ms=240, window_ms=2000,
        speaker_identifier=ident,
    )
    # 1s of speech at rel 0; gap heartbeats push rel_ts to 2000 -> 1000ms of
    # synthesized zeros -> utterance closes (560ms of zeros reached).
    out = pipe.ingest(pkt(0, 50, 0))
    assert ident.calls == []          # nothing closed yet, no per-hop embeds
    out = pipe.ingest(pkt(1, 0, 2000, vad=VadState.GAP_MARKER))
    all_t = out + pipe.flush()
    labelled = [t for t in all_t if t.text]
    assert len(ident.calls) == 1, "one fingerprint per utterance"
    # Gap fill stripped: the identified PCM is exactly the 1s of real speech.
    assert len(ident.calls[0]) == 50 * 640
    assert all(t.speaker == "spk-1" for t in labelled)
