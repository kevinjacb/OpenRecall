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
from ..protocol.messages import parse_control
from .core import GatewayCore, PipelineFactory

if TYPE_CHECKING:
    from ..commands.dispatcher import CommandDispatcher
    from ..events.store import EventStore
    from ..memory.extraction_worker import ExtractionEnqueuer
    from ..sessions.index import SessionIndex
    from ..sessions.lifecycle import SessionLifecycle
    from ..agent.proactive import ProactiveTriggerEngine
    from .core import ProactiveOutbox

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


def build_speaker_identifier(cfg, registry, embedder):
    """Return a :class:`SpeakerIdentifier` when enabled, else ``None``.

    The factory wires ``None`` into the pipeline when speaker ID is off, so
    the rollout guard is structural: zero embed calls and ``speaker=None`` on
    every Transcript when ``SENSE_SPEAKER_ENABLED=false``. ``embedder`` is
    either a real :class:`SpeakerEmbedder` or the sentinel ``"fake"`` (for
    tests / pre-hardware smoke); ``None`` defaults to the fake embedder so an
    enabled config without a wired backend still runs end-to-end.
    """
    if not cfg.enabled:
        return None
    from ..ingest.speaker_embedder import FakeSpeakerEmbedder
    from ..ingest.speaker_identifier import SpeakerIdentifier

    emb = (
        FakeSpeakerEmbedder(dim=16, min_speech_ms=cfg.min_speech_ms)
        if embedder == "fake" or embedder is None
        else embedder
    )
    return SpeakerIdentifier(emb, registry, cfg)


def build_pipeline_factory(
    window_ms: int = 5000,
    hop_ms: int = 1000,
    model: str | None = None,
    use_streaming: bool = True,
    whisper_config=None,
    gap_timeout_ms: int | None = 3000,
    speaker_identifier=None,
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

    ``whisper_config`` (a :class:`WhisperConfig`) threads the
    server-side noise filtering thresholds (no_speech_threshold,
    logprob_threshold) into the backend. Defaults to mlx-whisper's own
    defaults (0.6 / -1.0) if not provided.

    Set ``use_streaming=False`` for the legacy hard-cut path (only used
    by tests that pre-date the streaming work).
    """

    no_speech_threshold = (
        whisper_config.no_speech_threshold
        if whisper_config is not None
        else 0.6
    )
    logprob_threshold = (
        whisper_config.logprob_threshold
        if whisper_config is not None
        else -1.0
    )

    def factory(start_seq: int) -> AudioIngestPipeline:
        from ..ingest.opus_decoder import OpusStreamDecoder
        from ..ingest.streaming_transcriber import streaming_from_tokens
        from ..ingest.whisper_mlx import MlxWhisperTranscriber
        from ..ingest.whisper_streaming import WhisperStreamingBackend

        if use_streaming:
            # The mlx_whisper call is the slow part (Whisper inference).
            # In production each session gets its own backend (so the
            # internal numpy buffers are session-scoped), but the
            # underlying model is shared implicitly via mlx-whisper's
            # module-level state. The streaming transcriber owns the
            # rolling PCM buffer and committed cursor.
            transcriber = streaming_from_tokens(
                WhisperStreamingBackend(
                    model=model,
                    no_speech_threshold=no_speech_threshold,
                    logprob_threshold=logprob_threshold,
                ) if model else WhisperStreamingBackend(
                    no_speech_threshold=no_speech_threshold,
                    logprob_threshold=logprob_threshold,
                ),
                sample_rate=16000,
                hop_ms=hop_ms,
                window_ms=window_ms,
            )
        else:
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
        core = GatewayCore(
            pipeline_factory=pipeline_factory,
            event_store=event_store,
            dispatcher=dispatcher,
            session_index=session_index,
            session_lifecycle=session_lifecycle,
            enqueuer=enqueuer,
            proactive_outbox=proactive_outbox,
        )
        # P3: rebind the engine's ws_sender to this per-connection core.
        # The engine is process-wide (one Planner, one set of listeners),
        # but every WebSocket connection has its own core, so we re-target
        # on connect. None-safe for back-compat with callers that don't
        # have an engine wired (existing test suite).
        if proactive_engine is not None and proactive_outbox is not None:
            proactive_engine.set_ws_sender(core)

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
        try:
            async for message in ws:
                # Decode + transcription are blocking and can take seconds (model
                # load/compile on the first window). Offload to a worker thread so the
                # event loop stays responsive (keepalive pings, other connections) and
                # the connection doesn't time out mid-transcribe. Messages from one
                # connection are still processed in order (we await each in turn).
                try:
                    replies = await asyncio.to_thread(handle_message, core, message)
                except GatewayError:
                    # Protocol violation — re-raise so the outer handler closes 1002.
                    raise
                except Exception:
                    # A bad packet / decode / transcribe error must NOT tear down the
                    # relay connection. Log it and keep going — one bad frame shouldn't
                    # reset the link. (GatewayError above is re-raised to the outer try.)
                    kind = "binary" if isinstance(message, (bytes, bytearray, memoryview)) else "text"
                    logger.exception("error handling a %s frame (%d bytes) from %s; skipping",
                                     kind, len(message), peer)
                    continue
                for reply in replies:
                    await ws.send(reply)
        except GatewayError as exc:
            logger.warning("protocol error from %s — closing 1002: %s", peer, exc)
            await ws.close(code=1002, reason="protocol error")  # 1002 == protocol error
        except ConnectionClosed:
            pass  # client went away (possibly mid-transcribe) — a normal disconnect
        finally:
            # Bug C: a relay disconnect without a clean bye leaves the live
            # extraction cursor holding the trailing partial 60s window back.
            # Finalize the session this core was carrying so the last ~up-to-60s
            # of speech forms memories without waiting for a server restart.
            # No-op if the client already said bye (bye enqueued its own
            # finalize and cleared the session id) or never said hello.
            core.finalize_pending_session()
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
