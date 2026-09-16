"""WebSocket transport adapter for the gateway.

Two layers:

* :func:`handle_message` — the unit-tested router: text frame -> §E control,
  binary frame -> §C.6 audio, returning outbound §E messages as JSON strings.
* :func:`serve` — the async socket loop, a thin shell that moves bytes and calls
  the router. One :class:`GatewayCore` per connection.

This is the BLE-relayed control+audio plane (XIAO -> Android -> Mac). WiFi is not
used here; it is reserved for end-of-day bulk video retrieval.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..ingest.pipeline import AudioIngestPipeline
from ..ingest.reassembler import SessionReassembler
from ..ingest.shared_asr_model import get_shared_model
from ..protocol.messages import parse_control
from .core import GatewayCore, PipelineFactory

if TYPE_CHECKING:
    from ..commands.dispatcher import CommandDispatcher
    from ..events.store import EventStore
    from ..memory.extraction_worker import ExtractionEnqueuer
    from ..sessions.index import SessionIndex
    from ..sessions.lifecycle import SessionLifecycle
    from ..agent.proactive import ProactiveTriggerEngine
    from ..agent.speaker_nudge import SpeakerNudgeListener
    from ..memory.speaker_registry import SpeakerRegistry
    from ..memory.store import AtomStore
    from ..sessions.segments import SegmentIndex
    from ..settings.reconciler import DeviceReconciler
    from ..settings.store import SettingsStore
    from ..contracts.types import CapabilityProvider
    from .core import ProactiveOutbox
    from .liveness import DeviceLiveness

logger = logging.getLogger(__name__)


def _monotonic_ms() -> float:
    """A monotonic clock in milliseconds for the reassembler's gap deadline."""
    return time.monotonic() * 1000.0


def handle_message(core: GatewayCore, message: str | bytes) -> list[str]:
    """Route one inbound frame to the core; return outbound §E messages as JSON."""
    if isinstance(message, (bytes, bytearray, memoryview)):
        outbound = core.on_audio(bytes(message))
    else:
        outbound = core.on_control(parse_control(message))
    return [m.model_dump_json() for m in outbound]


def _select_embedder(cfg):
    """Backwards-compatible alias for ``ingest.speaker_embedder.select_embedder``.

    The rule moved down to the module that owns both implementations so the
    inference service can reuse it without importing the gateway. Kept here
    because existing call sites and tests reference this name.
    """
    from ..ingest.speaker_embedder import select_embedder

    return select_embedder(cfg)


def _remote_url(inference_config) -> "str | None":
    """The configured inference-service URL, or ``None`` for in-process.

    One place decides what "remote" means, so the ASR and embedder sites
    cannot drift apart — and callers that predate P1 (every existing test)
    pass nothing and get the in-process path.
    """
    return None if inference_config is None else inference_config.url


def build_speaker_identifier(cfg, registry, embedder=None, inference_config=None):
    """Return a :class:`SpeakerIdentifier` when enabled, else ``None``.

    The factory wires ``None`` into the pipeline when speaker ID is off, so
    the rollout guard is structural: zero embed calls and ``speaker=None`` on
    every Transcript when ``OPENRECALL_SPEAKER_ENABLED=false``.

    ``embedder``: ``None`` -> select from ``cfg.embed_model`` (``"resemblyzer"``
    -> the real backend, ``""``/``"fake"`` -> the deterministic fake); the
    sentinel ``"fake"`` -> the fake (explicit test override); a concrete
    :class:`SpeakerEmbedder` -> used as-is.

    ``inference_config`` (P1): with a url set, the embedder is the HTTP client
    instead — same Protocol, model in the other process. Only consulted when
    ``embedder is None``; an explicitly supplied embedder always wins.
    """
    if not cfg.enabled:
        return None
    from ..ingest.speaker_identifier import SpeakerIdentifier

    if embedder is None:
        url = _remote_url(inference_config)
        if url is None:
            embedder = _select_embedder(cfg)
        else:
            from ..inference.client import HttpSpeakerEmbedder

            embedder = HttpSpeakerEmbedder(
                url, timeout_s=inference_config.timeout_s)
    elif embedder == "fake":
        from ..ingest.speaker_embedder import FakeSpeakerEmbedder

        embedder = FakeSpeakerEmbedder(dim=16, min_speech_ms=cfg.min_speech_ms)
    return SpeakerIdentifier(embedder, registry, cfg)


def build_pipeline_factory(
    window_ms: int = 5000,
    hop_ms: int = 1000,
    model: str | None = None,
    use_streaming: bool = True,
    whisper_config=None,
    asr_config=None,
    gap_timeout_ms: int | None = 3000,
    speaker_identifier=None,
    speaker_window_ms: int = 2000,
    audio_store=None,
    persist_audio: bool = True,
    audio_enabled=None,
    rel_ts_sink=None,
    sentence_coalesce: bool = True,
    sentence_pause_ms: int | None = None,
    denoiser=None,
    inference_config=None,
) -> PipelineFactory:
    """Factory wiring the real Opus decoder + MLX-whisper transcriber per session.

    Heavy deps (opuslib, mlx-whisper) are imported lazily here so importing the
    gateway never requires them; only actually serving live audio does.

    When ``use_streaming`` is True (the default), the factory builds a
    :class:`WhisperStreamingBackend` (mlx-whisper with
    ``word_timestamps=True``) wrapped in a :class:`StreamingTranscriber`
    via :func:`streaming_from_tokens`. This eliminates the boundary-loss
    artifacts of the hard-cut 5s window pipeline: every hop re-runs
    Whisper on a rolling 5s context, and the streaming wrapper dedups
    tokens whose start is past the committed cursor.

    ``asr_config`` (an :class:`AsrConfig`) selects *which* streaming
    backend gets built. ``backend="whisper"`` (the default, and what
    ``asr_config=None`` means) builds the mlx-whisper path described
    above, unchanged. ``backend="parakeet"`` builds a
    :class:`ParakeetStreamingBackend` instead — NVIDIA Parakeet-TDT via
    parakeet-mlx, whose transducer decoder emits a blank symbol on
    silence and so does not hallucinate phantom phrases on room noise.
    ``backend="faster_whisper"`` builds a
    :class:`FasterWhisperStreamingBackend` — the same Whisper decoder as the
    default, on CTranslate2 instead of MLX, so it runs on plain CPU and on
    CUDA rather than requiring Apple Silicon. Being Whisper, it takes every
    Whisper-specific noise filter below, unlike Parakeet.

    The backends satisfy the same ``StreamingBackend`` Protocol, so
    everything above this seam (the rolling window, the committed
    cursor, the cross-hop dedup, the optional VAD gate) is identical
    either way. The Whisper-only noise filters below are simply not
    applicable to Parakeet and are not passed to it.

    ``whisper_config`` (a :class:`WhisperConfig`) threads the
    server-side noise filtering thresholds into the backend:
    ``no_speech_threshold`` / ``logprob_threshold`` (per-segment
    confidence), ``compression_ratio_threshold`` /
    ``condition_on_previous_text`` (anti-hallucination), the
    ``hallucination_blocklist`` (short-phrase phantom drop), and the
    opt-in ``vad_mode`` / ``vad_aggressiveness`` (pre-Whisper spectral
    VAD gate). Each defaults to the backend's own default when
    ``whisper_config`` is None.

    ``inference_config`` (an :class:`InferenceConfig`, P1) says *where* the
    selected backend runs. ``None`` or a ``url`` of ``None`` — the default —
    builds the in-process backend described above, unchanged. A url builds
    :class:`HttpStreamingBackend` instead: the same ``StreamingBackend``
    Protocol over HTTP, with the model living in the inference service. Which
    engine runs is then that process's business (it reads the same
    ``OPENRECALL_ASR_BACKEND``); scheduling — hop vs utterance, the rolling
    buffer, the committed cursor — stays here, above the seam.

    Set ``use_streaming=False`` for the legacy hard-cut path (only used
    by tests that pre-date the streaming work).
    """

    def _cfg(attr: str, default):
        # When a WhisperConfig is provided it always carries every field
        # (frozen, extra="forbid"), so a missing attr here is a typo — raise
        # rather than silently fall back to the default. The default only
        # applies when whisper_config is None (no config supplied).
        if whisper_config is None:
            return default
        return getattr(whisper_config, attr)

    no_speech_threshold = _cfg("no_speech_threshold", 0.6)
    logprob_threshold = _cfg("logprob_threshold", -1.0)
    compression_ratio_threshold = _cfg("compression_ratio_threshold", 2.4)
    condition_on_previous_text = _cfg("condition_on_previous_text", False)
    hallucination_blocklist_enabled = _cfg("hallucination_blocklist_enabled", True)
    hallucination_max_words = _cfg("hallucination_max_words", 4)
    hallucination_phrases = _cfg("hallucination_phrases", None)
    vad_mode = _cfg("vad_mode", None)
    vad_aggressiveness = _cfg("vad_aggressiveness", 3)

    # Backend selection is resolved here, at factory-build time, so a bad
    # value fails at startup rather than on the first audio packet. Same
    # fail-fast getattr reasoning as _cfg above.
    if asr_config is None:
        asr_backend = "whisper"
        parakeet_model = None
        faster_whisper_model = None
        faster_whisper_device = None
        faster_whisper_compute_type = None
        asr_mode = "hop"
    else:
        asr_backend = asr_config.backend
        parakeet_model = asr_config.parakeet_model
        faster_whisper_model = asr_config.faster_whisper_model
        faster_whisper_device = asr_config.faster_whisper_device
        faster_whisper_compute_type = asr_config.faster_whisper_compute_type
        asr_mode = asr_config.resolved_mode()

    # Where that backend runs. Resolved here for the same reason as above: one
    # read at build time, so a typo'd attribute raises at startup rather than
    # on the first audio packet. None (no config, or no url) == in-process.
    inference_url = _remote_url(inference_config)
    inference_timeout_s = (
        inference_config.timeout_s if inference_url is not None else None
    )

    # Raised here, at factory-build time, not per session: the legacy hard-cut
    # path needs a str-returning `Transcriber`, while the HTTP client implements
    # the token-returning `StreamingBackend`. So a url set alongside
    # use_streaming=False would silently run inference in-process — the one
    # combination where the config says "remote" and the behaviour is not.
    # No production caller passes use_streaming=False today (only two legacy
    # tests do), which is exactly why this should be a guard rather than a
    # comment: it has to still hold if someone revives that path.
    if inference_url is not None and not use_streaming:
        raise ValueError(
            "inference_config.url is set but use_streaming=False: the legacy "
            "hard-cut path takes a str-returning Transcriber and would run "
            "in-process, silently ignoring the configured inference service."
        )

    def factory(start_seq: int) -> AudioIngestPipeline:
        from ..ingest.opus_decoder import OpusStreamDecoder
        from ..ingest.streaming_transcriber import streaming_from_tokens

        if use_streaming:
            # ---- in-process (the default, and the P1 rollback) ----------
            if inference_url is None:
                from ..ingest.whisper_streaming import (
                    DEFAULT_MODEL as _whisper_default_model,
                )
                from ..ingest.whisper_streaming import WhisperStreamingBackend

                # The model call is the slow part (ASR inference). Each session
                # gets its own backend (so the internal buffers are session-
                # scoped), but the underlying model is shared by injection from
                # a process-wide cache (ingest.shared_asr_model). The first
                # session loads + warms the model; reconnects hit the cache and
                # reuse the same object — no reload per session. A shared
                # threading.Lock on the holder serializes inference (acquired by
                # the inference worker in Task 3). The streaming transcriber
                # owns the rolling PCM buffer and committed cursor, identically
                # for both backends.
                if asr_backend == "parakeet":
                    from ..ingest.parakeet_streaming import (
                        DEFAULT_PARAKEET_MODEL,
                        ParakeetStreamingBackend,
                        load_model,
                        warmup_model,
                    )

                    # THREAD AFFINITY INVARIANT — do not move this load earlier.
                    # MLX arrays belong to the thread that created them: a model
                    # loaded on one thread and invoked on another raises
                    # `RuntimeError: There is no Stream(gpu, 0) in current thread`.
                    # This factory runs inside `_on_hello`, which the single
                    # `asr-worker` thread reaches via handle_message — the same
                    # thread that later calls `transcribe`. That coincidence is
                    # what makes Parakeet work, so it is load-bearing.
                    #
                    # Concretely: do NOT hoist this into an eager startup warmup on
                    # the event loop thread, and do not spread inference across a
                    # pool. Both break Parakeet at runtime with a green test suite,
                    # because the fakes used in tests have no thread affinity.
                    # (Observed 2026-09-17 with parakeet-mlx 0.5 / mlx 0.31 while
                    # building the out-of-process inference service, which has to
                    # pin each model to one dedicated thread for this reason.)
                    #
                    # `model` (the --model CLI flag) is the mlx-whisper repo
                    # override and is deliberately NOT reused here: pointing the
                    # Parakeet loader at a Whisper repo would fail confusingly.
                    # The Parakeet repo has its own knob (OPENRECALL_PARAKEET_MODEL).
                    _parakeet_name = parakeet_model or DEFAULT_PARAKEET_MODEL
                    parakeet_kwargs = {}
                    try:
                        _shared = get_shared_model(
                            _parakeet_name, loader=load_model, warmup=warmup_model,
                        )
                        parakeet_kwargs["model"] = _shared.model
                        # R4: pass the shared inference lock so concurrent
                        # connections serialize model.generate calls.
                        parakeet_kwargs["inference_lock"] = _shared.inference_lock
                    except ImportError:
                        # parakeet_mlx not installed (test environment); fall
                        # back to the backend's own lazy load on first transcribe.
                        pass
                    if parakeet_model:
                        parakeet_kwargs["model_name"] = parakeet_model
                    backend = ParakeetStreamingBackend(**parakeet_kwargs)
                elif asr_backend == "faster_whisper":
                    from ..ingest.faster_whisper_streaming import (
                        DEFAULT_COMPUTE_TYPE,
                        DEFAULT_DEVICE,
                        DEFAULT_FASTER_WHISPER_MODEL,
                        FasterWhisperStreamingBackend,
                        load_model as _load_faster_whisper,
                    )
                    from ..ingest.faster_whisper_streaming import (
                        warmup_model as _warmup_faster_whisper,
                    )

                    # Whisper's decoder on CTranslate2 — the same model family
                    # as the mlx path above, but it runs on plain CPU and on
                    # CUDA, which is the whole reason this backend exists. The
                    # Whisper-specific noise filters therefore apply verbatim
                    # and are passed through.
                    #
                    # Unlike the Parakeet branch above there is no thread-
                    # affinity invariant to preserve: CTranslate2 documents the
                    # model as callable from several Python threads (that is
                    # what its `num_workers` parallelizes), so loading here and
                    # inferring elsewhere is supported. The shared lock below
                    # is kept anyway, to serialize sessions onto one model for
                    # memory rather than for correctness.
                    _fw_name = faster_whisper_model or DEFAULT_FASTER_WHISPER_MODEL
                    _fw_device = faster_whisper_device or DEFAULT_DEVICE
                    _fw_compute = faster_whisper_compute_type or DEFAULT_COMPUTE_TYPE
                    fw_kwargs = dict(
                        model=_fw_name,
                        device=_fw_device,
                        compute_type=_fw_compute,
                        no_speech_threshold=no_speech_threshold,
                        logprob_threshold=logprob_threshold,
                        compression_ratio_threshold=compression_ratio_threshold,
                        condition_on_previous_text=condition_on_previous_text,
                        hallucination_blocklist_enabled=hallucination_blocklist_enabled,
                        hallucination_max_words=hallucination_max_words,
                        hallucination_phrases=hallucination_phrases,
                    )
                    try:
                        # The cache key carries device + compute_type: the same
                        # weights quantized differently are different loaded
                        # models, and the holder is keyed by string alone.
                        _shared = get_shared_model(
                            f"faster-whisper:{_fw_name}:{_fw_device}:{_fw_compute}",
                            loader=lambda _key: _load_faster_whisper(
                                _fw_name,
                                device=_fw_device,
                                compute_type=_fw_compute,
                            ),
                            warmup=_warmup_faster_whisper,
                        )
                        fw_kwargs["loaded_model"] = _shared.model
                        fw_kwargs["inference_lock"] = _shared.inference_lock
                    except ImportError:
                        # faster-whisper not installed (test environment); fall
                        # back to the backend's own lazy load on first
                        # transcribe, which raises there with a clear message.
                        pass
                    backend = FasterWhisperStreamingBackend(**fw_kwargs)
                else:
                    # Whisper's "model" is the repo-name string; mlx_whisper
                    # caches the loaded weights at module level internally, so
                    # the loader is the identity function. Routing through the
                    # shared holder gives a uniform seam + the shared inference
                    # lock, so a backend switch never regresses this.
                    _whisper_name = model or _whisper_default_model
                    _shared = get_shared_model(_whisper_name, loader=lambda n: n)
                    backend_kwargs = dict(
                        no_speech_threshold=no_speech_threshold,
                        logprob_threshold=logprob_threshold,
                        compression_ratio_threshold=compression_ratio_threshold,
                        condition_on_previous_text=condition_on_previous_text,
                        hallucination_blocklist_enabled=hallucination_blocklist_enabled,
                        hallucination_max_words=hallucination_max_words,
                        hallucination_phrases=hallucination_phrases,
                        model=_shared.model,
                        # R4: pass the shared inference lock so concurrent
                        # connections serialize model.generate calls.
                        inference_lock=_shared.inference_lock,
                    )
                    backend = WhisperStreamingBackend(**backend_kwargs)
            else:
                # ---- remote: the model lives in the inference service ---
                # Same StreamingBackend Protocol, so everything below this
                # line — the rolling window, the committed cursor, the
                # cross-hop dedup, the utterance segmentation — is identical
                # either way. Nothing in the in-process model path (the
                # shared-model cache, the mlx imports it triggers) is
                # touched, which is what lets the gateway run where MLX and
                # CUDA are not installed.
                from ..inference.client import HttpStreamingBackend

                backend = HttpStreamingBackend(
                    inference_url, timeout_s=inference_timeout_s)
            if asr_mode == "utterance":
                # Utterance mode (the parakeet default): buffer speech and
                # transcribe each utterance ONCE when the wearer pauses. No
                # overlapping re-transcription — Parakeet's token timestamps
                # shift between overlapping rolling-window calls, which
                # defeated the streaming dedup and interleaved duplicate
                # fragments into the transcript (real-device evidence
                # 2026-08-29). One call per utterance is also ~10x less
                # compute than the 240 ms hop cadence.
                from ..ingest.utterance_transcriber import UtteranceTranscriber

                transcriber = UtteranceTranscriber(
                    backend, sample_rate=16000, hop_ms=hop_ms,
                )
            else:
                transcriber = streaming_from_tokens(
                    backend,
                    sample_rate=16000,
                    hop_ms=hop_ms,
                    window_ms=window_ms,
                    vad_mode=vad_mode,
                    vad_aggressiveness=vad_aggressiveness,
                    # Skip the ASR call on hops that are pure synthesized gap
                    # silence once the rolling window is fully silent — reclaims
                    # the extra calls the pipeline's gap-silence fill would cost.
                    skip_silent_windows=True,
                )
        else:
            # Legacy hard-cut path, in-process only: it wants a str-returning
            # Transcriber, and the HTTP client implements the token-returning
            # StreamingBackend Protocol instead. Only tests that pre-date the
            # streaming work reach this; production always streams, so the
            # inference boundary has nothing to cross here.
            from ..ingest.whisper_mlx import MlxWhisperTranscriber

            transcriber = (
                MlxWhisperTranscriber(model) if model else MlxWhisperTranscriber()
            )
        return AudioIngestPipeline(
            reassembler=SessionReassembler(
                start_seq=start_seq,
                gap_timeout_ms=gap_timeout_ms,
                # Bug A: a monotonic-ms clock so an unfillable head gap (lost
                # chunks after a reconnect) is skipped after gap_timeout_ms
                # instead of stalling the stream forever.
                clock=_monotonic_ms,
            ),
            decoder=OpusStreamDecoder(),
            transcriber=transcriber,
            hop_ms=hop_ms,
            window_ms=window_ms,
            sample_rate=16000,
            speaker_identifier=speaker_identifier,
            speaker_window_ms=speaker_window_ms,
            # Spec §3.1. The session id is bound later, on `hello`, via
            # GatewayCore -> AudioIngestPipeline.set_audio_target.
            audio_store=audio_store,
            persist_audio=persist_audio,
            audio_enabled=audio_enabled,
            rel_ts_sink=rel_ts_sink,
            sentence_coalesce=sentence_coalesce,
            sentence_pause_ms=(
                sentence_pause_ms if sentence_pause_ms is not None else 400
            ),
            denoiser=denoiser,
        )

    return factory


async def serve(
    pipeline_factory: PipelineFactory,
    host: str = "0.0.0.0",
    port: int = 8765,
    event_store: "EventStore | None" = None,
    dispatcher: "CommandDispatcher | None" = None,
    token: str | None = None,
    session_index: "SessionIndex | None" = None,
    session_lifecycle: "SessionLifecycle | None" = None,
    enqueuer: "ExtractionEnqueuer | None" = None,
    proactive_outbox: "ProactiveOutbox | None" = None,
    proactive_engine: "ProactiveTriggerEngine | None" = None,
    speaker_registry: "SpeakerRegistry | None" = None,
    atom_store: "AtomStore | None" = None,
    speaker_nudge: "SpeakerNudgeListener | None" = None,
    segment_index: "SegmentIndex | None" = None,
    reconciler: "DeviceReconciler | None" = None,
    liveness: "DeviceLiveness | None" = None,
    capability_provider: "CapabilityProvider | None" = None,
    settings: "SettingsStore | None" = None,
    command_detector=None,
    command_memory_writer=None,
) -> None:
    """Run the gateway WebSocket server until cancelled.

    A single ``event_store`` and ``dispatcher`` are shared by every connection's core
    (one durable log; one command registry). When ``token`` is not ``None``, each
    inbound connection must present an ``Authorization: Bearer <token>`` header or
    it is closed with a 1008 "unauthorized" policy error before any §E processing.
    A ``None`` token disables auth (local dev / existing tests).

    ``session_index`` and ``session_lifecycle`` are the Phase 3 dependencies
    that back the HTTP ``/sessions`` and ``/status`` routes: the index is fed
    by every successfully-stored capture event, and the lifecycle tracks
    currently-open connections. Both are optional for back-compat with the
    existing test suite.

    ``proactive_outbox`` and ``proactive_engine`` (P3): per-connection, the
    core is bound to a shared ``ProactiveOutbox`` and the engine's
    ``ws_sender`` is rebound to that core. A background ``proactive_task``
    on each connection drains pending ProactiveMessages and writes them as
    ``proactive`` §E frames. Both are optional — when omitted, the
    per-connection core has no outbox and ``send_proactive`` raises
    rather than silently dropping.
    """
    import asyncio

    import websockets
    from websockets.exceptions import ConnectionClosed

    from ..http.token import constant_time_eq
    from .core import GatewayError
    from .inference_worker import InferenceWorker

    async def handler(ws: "websockets.ServerConnection") -> None:
        peer = getattr(ws, "remote_address", None)
        # --- bearer-token check (skip when token is None: local dev) ---
        if token is not None:
            auth = ws.request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not constant_time_eq(auth[7:], token):
                logger.warning("rejecting connection from %s: bad/missing bearer token", peer)
                await ws.close(code=1008, reason="unauthorized")
                return
        logger.info("connection opened from %s", peer)
        # Counted after the auth check, so a rejected connection never makes
        # /device/status claim the relay is connected (spec §5.1).
        if liveness is not None:
            liveness.connection_opened()
        core = GatewayCore(
            pipeline_factory=pipeline_factory,
            event_store=event_store,
            dispatcher=dispatcher,
            session_index=session_index,
            session_lifecycle=session_lifecycle,
            enqueuer=enqueuer,
            proactive_outbox=proactive_outbox,
            speaker_registry=speaker_registry,
            atom_store=atom_store,
            segment_index=segment_index,
            reconciler=reconciler,
            liveness=liveness,
            capability_provider=capability_provider,
            settings=settings,
            command_detector=command_detector,
            command_memory_writer=command_memory_writer,
        )
        # P3: rebind the engine's ws_sender to this per-connection core.
        # The engine is process-wide (one Planner, one set of listeners),
        # but every WebSocket connection has its own core, so we re-target
        # on connect. None-safe for back-compat with callers that don't
        # have an engine wired (existing test suite).
        if proactive_engine is not None and proactive_outbox is not None:
            proactive_engine.set_ws_sender(core)
        # Speaker nudge: rebind its ws_sender to this per-connection core
        # too, so the nudge is pushed through the active connection.
        if speaker_nudge is not None:
            speaker_nudge.set_ws_sender(core)

        # P3: background drain task — wakes on the outbox event, sends
        # each pending ProactiveMessage as a §E frame. Cancelled on
        # connection close.
        proactive_task: asyncio.Task[None] | None = None
        if proactive_outbox is not None:
            async def _drain_proactive() -> None:
                # session_id is fixed for the life of this connection;
                # the engine always sends to the session it just
                # completed extraction for. If the client hasn't said
                # hello yet, we still drain any orphan messages (they
                # will simply be the empty bucket).
                while True:
                    await proactive_outbox.wait()
                    # drain_all() clears the wake event *before* draining, so
                    # a bare signal() (e.g. the connection-close wake below)
                    # with no pending message does not leave the event set —
                    # which would make wait() return immediately forever
                    # (asyncio.Event.wait() does not yield when already set)
                    # and hot-spin at 100% CPU, starving the event loop (the
                    # 'works for ~20s then every connection times out, no
                    # error in the terminal' incident). In practice each
                    # connection has at most one active session, so the cost
                    # is O(1).
                    drained = proactive_outbox.drain_all()
                    if drained:
                        logger.info(
                            "proactive_drain peer=%s messages=%d", peer, len(drained),
                        )
                    for msg in drained:
                        try:
                            await ws.send(msg.model_dump_json())
                            logger.info(
                                "proactive_sent peer=%s session=%s text=%r",
                                peer, msg.session_id, msg.text[:200],
                            )
                        except ConnectionClosed:
                            return
                        except Exception:
                            logger.exception(
                                "proactive_send_failed peer=%s", peer,
                            )
                            return
            proactive_task = asyncio.create_task(_drain_proactive())
        # S1: decouple inference from the WS receive loop. One InferenceWorker
        # (daemon thread + bounded queue.Queue) processes handle_message calls
        # off the event loop so a slow transcribe doesn't head-of-line-block
        # keepalive pings or subsequent packet replies. The WS loop enqueues
        # messages (non-blocking); a reply-sender task drains the loop-side
        # asyncio.Queue and writes replies to the socket in order.
        #
        # Error contract (preserved exactly from the synchronous to_thread
        # path): GatewayError → re-raise → outer except closes 1002; any other
        # exception in handling a frame → log + skip that frame (the worker
        # does this internally, the link stays up); ConnectionClosed → pass.
        loop = asyncio.get_running_loop()
        reply_queue: asyncio.Queue = asyncio.Queue()

        def _on_result(item):
            """Sink for the worker — called on the event loop thread via
            ``call_soon_threadsafe``. Puts replies (list[str]) or a
            ``GatewayError`` onto the loop-side queue for the reply-sender."""
            reply_queue.put_nowait(item)

        worker = InferenceWorker(
            handle_fn=handle_message, core=core, loop=loop, sink=_on_result,
        )
        worker.start()

        async def _recv_loop():
            """Pull messages from ws and enqueue to the worker (non-blocking)."""
            async for message in ws:
                worker.enqueue(message)

        async def _send_replies():
            """Drain reply_queue and send replies in order. Re-raises
            ``GatewayError`` (posted by the worker) so the outer except
            closes the WebSocket with code 1002."""
            while True:
                item = await reply_queue.get()
                if isinstance(item, BaseException):
                    raise item
                for reply in item:
                    await ws.send(reply)

        recv_task: asyncio.Task[None] | None = None
        sender_task: asyncio.Task[None] | None = None
        try:
            recv_task = asyncio.create_task(_recv_loop())
            sender_task = asyncio.create_task(_send_replies())
            # Race the receive loop against the reply-sender. The first
            # to complete (connection closed, GatewayError, or send error)
            # wins; we surface its exception to the outer except blocks.
            done, _pending = await asyncio.wait(
                {recv_task, sender_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Prioritize GatewayError (→ close 1002) over ConnectionClosed
            # so a protocol violation always triggers the 1002 close even
            # if the socket also dropped at the same instant.
            gateway_exc: GatewayError | None = None
            other_exc: BaseException | None = None
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is None:
                    continue
                if isinstance(exc, GatewayError):
                    gateway_exc = exc
                elif other_exc is None:
                    other_exc = exc
            if gateway_exc is not None:
                raise gateway_exc
            if other_exc is not None:
                raise other_exc
        except GatewayError as exc:
            logger.warning("protocol error from %s — closing 1002: %s", peer, exc)
            await ws.close(code=1002, reason="protocol error")  # 1002 == protocol error
        except ConnectionClosed:
            pass  # client went away (possibly mid-transcribe) — a normal disconnect
        finally:
            # Stop the worker first so it doesn't process more messages or
            # post more replies after the IO tasks are torn down.
            await worker.stop()
            for t in (recv_task, sender_task):
                if t is not None and not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            # Bug C: a relay disconnect without a clean bye leaves the live
            # extraction cursor holding the trailing partial 60s window back.
            # Finalize the session this core was carrying so the last ~up-to-60s
            # of speech forms memories without waiting for a server restart.
            # No-op if the client already said bye (bye enqueued its own
            # finalize and cleared the session id) or never said hello.
            # on_disconnect also deregisters the session and stamps its
            # ended_at (spec §0.1/§0.2) — bye is unreliable on a socket
            # drop, so this block is the only close signal that always
            # fires.
            core.on_disconnect()
            if liveness is not None:
                liveness.connection_closed()
            if proactive_task is not None:
                # Cancel the drain task. We do NOT signal() the outbox here —
                # the event is shared process-wide, so a bare signal() on every
                # disconnect would wake every other connection's drain task for
                # a no-op cycle (a thundering-herd poke that fires on every relay
                # reconnect), and before the drain_all() fix a bare set with no
                # bucket to drain left the event set and hot-spun the loop.
                # task.cancel() injects CancelledError into the parked wait()
                # directly, so the event does not need to be set to observe the
                # cancellation promptly.
                proactive_task.cancel()
                try:
                    await proactive_task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("connection closed from %s", peer)

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever
