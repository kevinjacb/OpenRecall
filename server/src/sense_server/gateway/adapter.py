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
from typing import TYPE_CHECKING

from ..ingest.pipeline import AudioIngestPipeline
from ..ingest.reassembler import SessionReassembler
from ..protocol.messages import parse_control
from .core import GatewayCore, PipelineFactory

if TYPE_CHECKING:
    from ..commands.dispatcher import CommandDispatcher
    from ..events.store import EventStore
    from ..sessions.index import SessionIndex
    from ..sessions.lifecycle import SessionLifecycle

logger = logging.getLogger(__name__)


def handle_message(core: GatewayCore, message: str | bytes) -> list[str]:
    """Route one inbound frame to the core; return outbound §E messages as JSON."""
    if isinstance(message, (bytes, bytearray, memoryview)):
        outbound = core.on_audio(bytes(message))
    else:
        outbound = core.on_control(parse_control(message))
    return [m.model_dump_json() for m in outbound]


def build_pipeline_factory(
    window_ms: int = 5000,
    hop_ms: int = 1000,
    model: str | None = None,
) -> PipelineFactory:
    """Factory wiring the real Opus decoder + MLX-whisper transcriber per session.

    Heavy deps (opuslib, mlx-whisper) are imported lazily here so importing the
    gateway never requires them; only actually serving live audio does.
    """

    def factory(start_seq: int) -> AudioIngestPipeline:
        from ..ingest.opus_decoder import OpusStreamDecoder
        from ..ingest.whisper_mlx import MlxWhisperTranscriber

        transcriber = MlxWhisperTranscriber(model) if model else MlxWhisperTranscriber()
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=OpusStreamDecoder(),
            transcriber=transcriber,
            hop_ms=hop_ms,
            window_ms=window_ms,
            sample_rate=16000,
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
        )
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
        except GatewayError:
            logger.warning("protocol error from %s — closing 1002", peer)
            await ws.close(code=1002, reason="protocol error")  # 1002 == protocol error
        except ConnectionClosed:
            pass  # client went away (possibly mid-transcribe) — a normal disconnect
        logger.info("connection closed from %s", peer)

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever
