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


@pytest.fixture
def fake_faster_whisper_loader(monkeypatch):
    """Patch load_model+warmup_model so faster-whisper factory tests never
    construct a CTranslate2 model or download weights."""
    import openrecall_server.ingest.faster_whisper_streaming as fw

    monkeypatch.setattr(
        fw, "load_model", lambda name, device=None, compute_type=None: object())
    monkeypatch.setattr(fw, "warmup_model", lambda model: None)


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


# --- the portable backend (faster-whisper / CTranslate2) ---------------------


def test_faster_whisper_backend_is_built_when_selected(fake_faster_whisper_loader):
    """OPENRECALL_ASR_BACKEND=faster_whisper must reach the in-process path —
    this is the only backend a non-Apple box can run."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory
    from openrecall_server.ingest.faster_whisper_streaming import (
        FasterWhisperStreamingBackend,
    )

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "faster_whisper"})
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    backend = _streamer_backend(factory)
    assert isinstance(backend, FasterWhisperStreamingBackend)
    # The model comes from the shared cache, already loaded.
    assert backend._model is not None


def test_faster_whisper_device_and_compute_type_reach_the_backend(
    fake_faster_whisper_loader,
):
    """The device/compute_type pair is the portability switch; if the factory
    drops it, every deployment silently runs the default."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "faster_whisper",
        "OPENRECALL_FASTER_WHISPER_MODEL": "small.en",
        "OPENRECALL_FASTER_WHISPER_DEVICE": "cuda",
        "OPENRECALL_FASTER_WHISPER_COMPUTE_TYPE": "float16",
    })
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    backend = _streamer_backend(factory)
    assert backend._model_name == "small.en"
    assert backend._device == "cuda"
    assert backend._compute_type == "float16"


def test_faster_whisper_gets_the_whisper_noise_filters(fake_faster_whisper_loader):
    """It is the same Whisper decoder, so the server-side filters must be
    threaded in — dropping them would resurrect the phantom-phrase bug on
    every non-Apple deployment."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "faster_whisper",
        "OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD": "0.42",
        "OPENRECALL_WHISPER_HALLUCINATION_MAX_WORDS": "7",
    })
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    backend = _streamer_backend(factory)
    assert backend._no_speech_threshold == 0.42
    assert backend._hallucination_max_words == 7
    assert backend._hallucination_blocklist_enabled is True


def test_faster_whisper_runs_the_hop_path_not_utterance_mode(
    fake_faster_whisper_loader,
):
    """Whisper timestamps are stable across overlapping windows, so it takes
    the rolling-window streamer (Parakeet's utterance mode exists for the
    opposite reason)."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory
    from openrecall_server.ingest.streaming_transcriber import StreamingTranscriber

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "faster_whisper"})
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    assert isinstance(_raw_streamer(factory), StreamingTranscriber)


def test_whisper_model_flag_is_not_reused_for_faster_whisper(
    fake_faster_whisper_loader,
):
    """--model overrides the *mlx* repo id; a CT2 model id is a different
    namespace, so handing it over would fail confusingly."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "faster_whisper"})
    factory = build_pipeline_factory(
        model="mlx-community/whisper-small",
        whisper_config=cfg.whisper, asr_config=cfg.asr,
    )

    assert _streamer_backend(factory)._model_name == "large-v3-turbo"


def test_factory_shares_one_faster_whisper_model_across_sessions(monkeypatch):
    """Reconnects must reuse the loaded model, and the shared inference lock
    must reach the backend that serializes on it."""
    import openrecall_server.ingest.faster_whisper_streaming as fw

    loads: list[tuple] = []

    monkeypatch.setattr(
        fw, "load_model",
        lambda name, device=None, compute_type=None: (
            loads.append((name, device, compute_type)) or object()),
    )
    monkeypatch.setattr(fw, "warmup_model", lambda model: None)

    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "faster_whisper"})
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    backend1 = _streamer_backend(factory)
    backend2 = _streamer_backend(factory)

    assert backend1._model is backend2._model
    assert loads == [("large-v3-turbo", "auto", "default")]
    assert backend1._inference_lock is backend2._inference_lock
    assert backend1._inference_lock is not None


def test_faster_whisper_cache_entry_cannot_collide_with_the_whisper_path(
    fake_faster_whisper_loader,
):
    """The shared cache is keyed by a plain string, and the Whisper path stores
    the model *name* under its own key. If the faster-whisper key were just the
    model name, an operator naming both the same would hand the CT2 backend a
    str where a loaded model belongs — and it would fail on the first packet,
    in a worker thread."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    shared_name = "mlx-community/whisper-large-v3-turbo"
    # Populate the cache through the Whisper path first (its "model" is the
    # repo-name string itself).
    whisper_factory = build_pipeline_factory(model=shared_name)
    assert _streamer_backend(whisper_factory)._model == shared_name

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "faster_whisper",
        "OPENRECALL_FASTER_WHISPER_MODEL": shared_name,
    })
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    loaded = _streamer_backend(factory)._model
    assert not isinstance(loaded, str), "got the Whisper path's cache entry"


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


def test_vad_gate_applies_to_the_parakeet_hop_path_too(fake_parakeet_loader):
    """The backend-agnostic defenses live above the seam, so they must still
    be wired when Parakeet runs the rolling-window (hop) path. Utterance mode
    (the parakeet default) has no per-hop ASR call to gate — its min-speech
    check covers silence — so the gate only applies with mode=hop."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "parakeet",
        "OPENRECALL_ASR_MODE": "hop",
        "OPENRECALL_WHISPER_VAD_MODE": "webrtc",
    })
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)

    assert _raw_streamer(factory)._vad is not None


def test_parakeet_defaults_to_utterance_mode(fake_parakeet_loader):
    """Parakeet's default is utterance-mode transcription: one backend call
    per utterance, no overlapping re-transcription (the rolling-window dedup
    fails on Parakeet's unstable cross-call token timestamps — real-device
    evidence 2026-08-29)."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory
    from openrecall_server.ingest.utterance_transcriber import UtteranceTranscriber

    cfg = load_agent_config({"OPENRECALL_ASR_BACKEND": "parakeet"})
    assert cfg.asr.resolved_mode() == "utterance"
    factory = build_pipeline_factory(whisper_config=cfg.whisper, asr_config=cfg.asr)
    pipe = factory(0)
    assert isinstance(pipe._streamer, UtteranceTranscriber)


def test_whisper_defaults_to_hop_mode():
    """Whisper keeps the rolling-window path (its word timestamps are stable
    enough for the streaming dedup, and its filters are tuned for it)."""
    from openrecall_server.agent.config import load_agent_config

    cfg = load_agent_config({})
    assert cfg.asr.resolved_mode() == "hop"


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


# --- P1: in-process vs. remote inference ------------------------------------
#
# The default (no url) must stay byte-for-byte the existing path; a url must
# swap BOTH the ASR backend and the speaker embedder for the HTTP clients,
# without the in-process model ever being touched. That last property is what
# lets the gateway run where MLX/CUDA is not installed.


def _http_backend_cls():
    from openrecall_server.inference.client import HttpStreamingBackend

    return HttpStreamingBackend


def test_no_inference_url_keeps_the_in_process_backend():
    """An InferenceConfig with url unset is the default path — unchanged."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory
    from openrecall_server.ingest.whisper_streaming import WhisperStreamingBackend

    cfg = load_agent_config({})
    factory = build_pipeline_factory(
        whisper_config=cfg.whisper, asr_config=cfg.asr,
        inference_config=cfg.inference,
    )
    assert isinstance(_streamer_backend(factory), WhisperStreamingBackend)


def test_inference_url_selects_the_http_backend():
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({"OPENRECALL_INFERENCE_URL": "http://box:8767"})
    factory = build_pipeline_factory(
        whisper_config=cfg.whisper, asr_config=cfg.asr,
        inference_config=cfg.inference,
    )

    backend = _streamer_backend(factory)
    assert isinstance(backend, _http_backend_cls())
    assert backend._base_url == "http://box:8767"


def test_http_backend_carries_the_configured_timeout():
    """A client built without the configured timeout silently uses the
    client module's 30 s default, which is not what the operator asked for."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_pipeline_factory

    cfg = load_agent_config({
        "OPENRECALL_INFERENCE_URL": "http://box:8767",
        "OPENRECALL_INFERENCE_TIMEOUT_S": "12.5",
    })
    factory = build_pipeline_factory(
        whisper_config=cfg.whisper, asr_config=cfg.asr,
        inference_config=cfg.inference,
    )

    assert _streamer_backend(factory)._timeout_s == 12.5


def test_inference_url_never_constructs_the_in_process_backend(monkeypatch):
    """The point of the boundary: with a url set, nothing in the in-process
    model path runs — so the gateway can live in a container with no MLX and
    no CUDA.

    Asserting "no mlx import happened" would be VACUOUS here: the in-process
    Whisper path imports mlx lazily inside ``transcribe``, so building the
    factory imports no mlx either way. What is actually observable at build
    time is the in-process construction itself — the shared-model cache (which
    is what loads and warms a Parakeet model) and the two backend classes. Each
    is booby-trapped, so any of them being reached fails loudly.
    """
    import openrecall_server.gateway.adapter as adapter
    import openrecall_server.ingest.parakeet_streaming as ps
    import openrecall_server.ingest.whisper_streaming as ws

    def _boom(*a, **k):
        raise AssertionError("in-process model path was reached")

    monkeypatch.setattr(adapter, "get_shared_model", _boom)
    monkeypatch.setattr(ws, "WhisperStreamingBackend", _boom)
    monkeypatch.setattr(ps, "ParakeetStreamingBackend", _boom)

    from openrecall_server.agent.config import load_agent_config

    cfg = load_agent_config({"OPENRECALL_INFERENCE_URL": "http://box:8767"})
    factory = adapter.build_pipeline_factory(
        whisper_config=cfg.whisper, asr_config=cfg.asr,
        inference_config=cfg.inference,
    )

    assert isinstance(_streamer_backend(factory), _http_backend_cls())


def test_inference_url_wins_over_the_parakeet_backend_switch(monkeypatch):
    """Which engine runs is the inference service's business once the work is
    remote; OPENRECALL_ASR_BACKEND there selects it. The gateway must not try
    to load Parakeet locally as well."""
    import openrecall_server.gateway.adapter as adapter
    import openrecall_server.ingest.parakeet_streaming as ps

    def _boom(*a, **k):
        raise AssertionError("in-process model path was reached")

    monkeypatch.setattr(adapter, "get_shared_model", _boom)
    monkeypatch.setattr(ps, "ParakeetStreamingBackend", _boom)

    from openrecall_server.agent.config import load_agent_config

    cfg = load_agent_config({
        "OPENRECALL_ASR_BACKEND": "parakeet",
        "OPENRECALL_INFERENCE_URL": "http://box:8767",
    })
    factory = adapter.build_pipeline_factory(
        whisper_config=cfg.whisper, asr_config=cfg.asr,
        inference_config=cfg.inference,
    )

    # Scheduling stays the gateway's concern: parakeet still means utterance
    # mode, the backend behind it is just remote now.
    pipe = factory(0)
    from openrecall_server.ingest.utterance_transcriber import UtteranceTranscriber

    assert isinstance(pipe._streamer, UtteranceTranscriber)
    assert isinstance(pipe._streamer._backend, _http_backend_cls())


# --- P1: the speaker embedder crosses the same boundary ----------------------


def test_no_inference_url_keeps_the_in_process_embedder():
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_speaker_identifier
    from openrecall_server.ingest.speaker_config import SpeakerConfig
    from openrecall_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder
    from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry

    scfg = SpeakerConfig(enabled=True)
    cfg = load_agent_config({})
    ident = build_speaker_identifier(
        scfg, InMemorySpeakerRegistry(scfg), embedder=None,
        inference_config=cfg.inference,
    )
    assert isinstance(ident._embedder, ResemblyzerSpeakerEmbedder)


def test_inference_url_selects_the_http_embedder():
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_speaker_identifier
    from openrecall_server.inference.client import HttpSpeakerEmbedder
    from openrecall_server.ingest.speaker_config import SpeakerConfig
    from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry

    scfg = SpeakerConfig(enabled=True)
    cfg = load_agent_config({
        "OPENRECALL_INFERENCE_URL": "http://box:8767",
        "OPENRECALL_INFERENCE_TIMEOUT_S": "12.5",
    })
    ident = build_speaker_identifier(
        scfg, InMemorySpeakerRegistry(scfg), embedder=None,
        inference_config=cfg.inference,
    )

    assert isinstance(ident._embedder, HttpSpeakerEmbedder)
    assert ident._embedder._base_url == "http://box:8767"
    assert ident._embedder._timeout_s == 12.5


def test_explicit_embedder_still_wins_over_the_inference_url():
    """``embedder="fake"`` is the explicit test override; a url must not
    silently replace an embedder the caller handed in."""
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_speaker_identifier
    from openrecall_server.ingest.speaker_config import SpeakerConfig
    from openrecall_server.ingest.speaker_embedder import FakeSpeakerEmbedder
    from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry

    scfg = SpeakerConfig(enabled=True)
    cfg = load_agent_config({"OPENRECALL_INFERENCE_URL": "http://box:8767"})
    ident = build_speaker_identifier(
        scfg, InMemorySpeakerRegistry(scfg), embedder="fake",
        inference_config=cfg.inference,
    )
    assert isinstance(ident._embedder, FakeSpeakerEmbedder)


def test_a_url_with_the_legacy_path_is_refused_not_silently_local():
    """The one combination where the config says "remote" and the behaviour
    would not be: the legacy hard-cut path takes a str-returning Transcriber,
    but the HTTP client implements the token-returning StreamingBackend, so it
    cannot be used there. Refuse at build time rather than quietly running
    inference in-process."""
    import pytest

    from openrecall_server.agent.config import InferenceConfig
    from openrecall_server.gateway.adapter import build_pipeline_factory

    with pytest.raises(ValueError, match="use_streaming=False"):
        build_pipeline_factory(
            use_streaming=False,
            inference_config=InferenceConfig(url="http://box:8767"),
        )


def test_the_legacy_path_is_still_allowed_without_a_url():
    """The guard must not break the legacy path itself — only the impossible
    combination."""
    from openrecall_server.gateway.adapter import build_pipeline_factory

    assert build_pipeline_factory(use_streaming=False) is not None
