"""Tests for the websocket adapter's message router.

The router is the only unit-testable part of the transport shell: it maps an
inbound frame (text JSON control, or binary §C.6 audio) to a GatewayCore call and
serialises the outbound §E messages to JSON strings ready for ``ws.send``. The
async socket loop around it does nothing but move bytes.
"""

import json
import struct

import pytest

from openrecall_server.gateway.adapter import handle_message
from openrecall_server.gateway.core import GatewayCore
from openrecall_server.ingest.audio_packet import PacketType, VadState
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler


@pytest.fixture(autouse=True)
def _reset_shared_asr_cache():
    """Clear the process-wide ASR model cache before and after each test so
    a cached model from one test doesn't leak into another."""
    from openrecall_server.ingest.shared_asr_model import reset
    reset()
    yield
    reset()


@pytest.fixture
def fake_parakeet_loader(monkeypatch):
    """Patch load_model+warmup_model so parakeet factory tests don't download
    the real model from HuggingFace. Tests that need to count loads (e.g. the
    sharing test) do their own monkeypatch and don't use this fixture."""
    import openrecall_server.ingest.parakeet_streaming as ps

    monkeypatch.setattr(ps, "load_model", lambda name: object())
    monkeypatch.setattr(ps, "warmup_model", lambda model: None)


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    """Returns a distinct string per call so each hop emits a new segment.

    A constant string would be collapsed by the streaming transcriber's
    cross-hop short-phrase dedup (the phantom-repeat defense) — which is
    covered in test_streaming.py, not here. Here we want one transcript per
    hop to verify the routing/plumbing contract.
    """

    def __init__(self) -> None:
        self._n = 0

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self._n += 1
        return f"hello {self._n}"


def make_core(window_ms: int = 100) -> GatewayCore:
    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=FakeDecoder(),
            transcriber=FakeTranscriber(),
            hop_ms=20,
            window_ms=window_ms,
            sample_rate=16000,
        )

    return GatewayCore(pipeline_factory=factory)


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


def test_text_frame_is_routed_to_control_and_replies_are_json_strings():
    core = make_core()

    replies = handle_message(core, '{"type": "hello", "session_id": "s1", "start_seq": 0}')

    assert [json.loads(r) for r in replies] == [
        {"type": "ack", "session_id": "s1", "next_seq": 0}
    ]


def test_binary_frame_is_routed_to_audio():
    """Under the streaming pipeline, 5 frames @ hop=20ms = 5 transcripts.

    The str-returning FakeTranscriber returns a distinct string each call,
    so each hop produces a new committed segment (the str adapter's start_ms
    is the trailing edge of an ever-growing buffer, advancing past the
    committed cursor every hop).
    """
    core = make_core(window_ms=100)
    handle_message(core, '{"type": "hello", "session_id": "s1", "start_seq": 0}')

    replies = handle_message(core, audio_bytes(0, n_frames=5))

    parsed = [json.loads(r) for r in replies]
    transcripts = [m for m in parsed if m["type"] == "transcript"]
    acks = [m for m in parsed if m["type"] == "ack"]
    assert len(transcripts) == 5
    assert [t["text"] for t in transcripts] == [f"hello {i}" for i in range(1, 6)]
    assert all(t["duration_ms"] == 20 for t in transcripts)
    assert acks == [{"type": "ack", "session_id": "s1", "next_seq": 1}]


def test_build_pipeline_factory_threads_all_whisper_config_fields():
    """build_pipeline_factory reads every WhisperConfig field at call time
    (not deferred into the mlx-needing factory closure), so a typo in an
    attribute name raises immediately. This pins the threading and guards
    against silent default-fallback on a renamed field."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD": "0.7",
        "OPENRECALL_WHISPER_LOGPROB_THRESHOLD": "-0.8",
        "OPENRECALL_WHISPER_COMPRESSION_RATIO_THRESHOLD": "3.0",
        "OPENRECALL_WHISPER_CONDITION_ON_PREVIOUS_TEXT": "true",
        "OPENRECALL_WHISPER_HALLUCINATION_BLOCKLIST_ENABLED": "false",
        "OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS": "6",
        "OPENRECALL_WHISPER_HALLUCINATION_PHRASES": "thank you, hello",
        "OPENRECALL_WHISPER_VAD_MODE": "webrtc",
        "OPENRECALL_WHISPER_VAD_AGGRESSIVENESS": "2",
    })
    # Returns a factory closure; building it must not raise (all attr names
    # are valid). It must NOT call the mlx/opus lazy imports.
    factory = build_pipeline_factory(whisper_config=cfg.whisper)
    assert callable(factory)

    # No-config path (whisper_config=None) also yields a factory using defaults.
    assert callable(build_pipeline_factory(whisper_config=None))


# --- ASR backend switch (whisper <-> parakeet) ------------------------------


def _streamer_backend(factory):
    """Reach the StreamingBackend a factory's pipeline actually built.

    The production factory default wraps the streamer in a
    :class:`SentenceCoalescer`; unwrap it so these backend-wiring tests
    reach the real backend regardless of the coalescing layer.
    """
    from openrecall_server.ingest.sentence_coalescer import SentenceCoalescer

    pipeline = factory(0)
    streamer = pipeline._streamer
    if isinstance(streamer, SentenceCoalescer):
        streamer = streamer._streamer
    return streamer._backend


def _raw_streamer(factory):
    """Reach the raw :class:`StreamingTranscriber` a factory built."""
    from openrecall_server.ingest.sentence_coalescer import SentenceCoalescer

    streamer = factory(0)._streamer
    if isinstance(streamer, SentenceCoalescer):
        streamer = streamer._streamer
    return streamer


def test_default_factory_builds_the_whisper_backend():
    """No asr_config (and the default config) must keep the existing path —
    this is the guard that the switch changed nothing by default."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory
    from openrecall_server.ingest.whisper_streaming import WhisperStreamingBackend

    assert isinstance(_streamer_backend(build_pipeline_factory()), WhisperStreamingBackend)

    cfg = load_agent_config({})
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)
    assert isinstance(_streamer_backend(factory), WhisperStreamingBackend)


def test_parakeet_backend_is_built_when_selected(fake_parakeet_loader):
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory
    from openrecall_server.ingest.parakeet_streaming import ParakeetStreamingBackend

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "parakeet"})
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    backend = _streamer_backend(factory)
    assert isinstance(backend, ParakeetStreamingBackend)
    # The model is now injected from the shared cache (S4), not None as in
    # the old lazy-load path.
    assert backend._model is not None


def test_parakeet_model_override_reaches_the_backend(fake_parakeet_loader):
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "parakeet",
        "OPENRECALL_PARAKEET_MODEL": "mlx-community/parakeet-tdt-1.1b",
    })
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    assert _streamer_backend(factory)._model_name == "mlx-community/parakeet-tdt-1.1b"


def test_whisper_model_flag_is_not_reused_for_parakeet(fake_parakeet_loader):
    """--model overrides the mlx-whisper repo; handing it to the Parakeet
    loader would fail confusingly, so the flag must be ignored there."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "parakeet"})
    factory = build_pipeline_factory(
        model="mlx-community/whisper-small", whisper_config=cfg.whisper, asr_config=cfg.asr,
    )

    assert _streamer_backend(factory)._model_name == "mlx-community/parakeet-tdt-0.6b-v3"


def test_switching_back_to_whisper_restores_the_filters():
    """The revert path: flipping the env var back rebuilds the Whisper
    backend with its noise filters intact."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory
    from openrecall_server.ingest.whisper_streaming import WhisperStreamingBackend

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "whisper",
        "OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD": "0.7",
    })
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    backend = _streamer_backend(factory)
    assert isinstance(backend, WhisperStreamingBackend)
    assert backend._no_speech_threshold == 0.7


def test_vad_gate_applies_to_the_parakeet_backend_too(fake_parakeet_loader):
    """The backend-agnostic defenses live above the seam, so they must still
    be wired when Parakeet is selected."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "parakeet",
        "OPENRECALL_WHISPER_VAD_MODE": "webrtc",
    })
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    assert _raw_streamer(factory)._vad is not None


# --- S4: shared singleton ASR model (process-wide load-once cache) -----------


def test_factory_shares_one_parakeet_model_across_sessions(monkeypatch):
    """Two factory(start_seq) calls must receive the SAME model object, and
    the loader must be called exactly once total (the shared cache loads on
    the first call, hits on the second). Reconnects must not reload."""
    import openrecall_server.ingest.parakeet_streaming as ps

    loads: list[str] = []

    class FakeModel:
        def generate(self, mel):
            return []

    # Monkeypatch the loader so from_pretrained is never called (no
    # parakeet_mlx needed); the warmup is a no-op to avoid mlx imports.
    monkeypatch.setattr(ps, "load_model", lambda name: loads.append(name) or FakeModel())
    monkeypatch.setattr(ps, "warmup_model", lambda model: None)

    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "parakeet"})
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    backend1 = _streamer_backend(factory)  # factory(0) — first session, loads
    backend2 = _streamer_backend(factory)  # factory(1000) — reconnect, cache hit

    assert backend1._model is not None
    assert backend1._model is backend2._model  # same object identity
    assert len(loads) == 1  # loaded exactly once total


def test_factory_shares_one_whisper_model_entry_across_sessions():
    """The Whisper path also routes through the shared cache (uniform seam).
    Two factory calls get the same SharedAsrModel entry — and thus the same
    inference_lock — even though the "model" itself is just a repo-name string."""
    from openrecall_server.ingest.shared_asr_model import get_shared_model
    from openrecall_server.gateway.adapter import build_pipeline_factory

    factory = build_pipeline_factory()

    backend1 = _streamer_backend(factory)
    backend2 = _streamer_backend(factory)

    # Both backends received the same model name string from the cache.
    assert backend1._model == backend2._model
    # The shared holder entry (and its inference lock) is the same object.
    _whisper_default = "mlx-community/whisper-large-v3-turbo"
    shared1 = get_shared_model(_whisper_default, loader=lambda n: n)
    shared2 = get_shared_model(_whisper_default, loader=lambda n: n)
    assert shared1 is shared2
    assert shared1.inference_lock is shared2.inference_lock
