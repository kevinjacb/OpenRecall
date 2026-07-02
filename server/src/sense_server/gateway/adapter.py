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

from typing import TYPE_CHECKING

from ..ingest.pipeline import AudioIngestPipeline
from ..ingest.reassembler import SessionReassembler
from ..protocol.messages import parse_control
from .core import GatewayCore, PipelineFactory

if TYPE_CHECKING:
    from ..commands.dispatcher import CommandDispatcher
    from ..events.store import EventStore


def handle_message(core: GatewayCore, message: str | bytes) -> list[str]:
    """Route one inbound frame to the core; return outbound §E messages as JSON."""
    if isinstance(message, (bytes, bytearray, memoryview)):
        outbound = core.on_audio(bytes(message))
    else:
        outbound = core.on_control(parse_control(message))
    return [m.model_dump_json() for m in outbound]


def build_pipeline_factory(window_ms: int = 5000, model: str | None = None) -> PipelineFactory:
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
) -> None:
    """Run the gateway WebSocket server until cancelled.

    A single ``event_store`` and ``dispatcher`` are shared by every connection's core
    (one durable log; one command registry). When ``token`` is not ``None``, each
    inbound connection must present an ``Authorization: Bearer <token>`` header or
    it is closed with a 1008 "unauthorized" policy error before any §E processing.
    A ``None`` token disables auth (local dev / existing tests).
    """
    import asyncio

    import websockets
    from websockets.exceptions import ConnectionClosed

    from ..http.token import constant_time_eq
    from .core import GatewayError

    async def handler(ws: "websockets.ServerConnection") -> None:
        # --- bearer-token check (skip when token is None: local dev) ---
        if token is not None:
            auth = ws.request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not constant_time_eq(auth[7:], token):
                await ws.close(code=1008, reason="unauthorized")
                return
        core = GatewayCore(
            pipeline_factory=pipeline_factory,
            event_store=event_store,
            dispatcher=dispatcher,
        )
        try:
            async for message in ws:
                # Decode + transcription are blocking and can take seconds (model
                # load/compile on the first window). Offload to a worker thread so the
                # event loop stays responsive (keepalive pings, other connections) and
                # the connection doesn't time out mid-transcribe. Messages from one
                # connection are still processed in order (we await each in turn).
                replies = await asyncio.to_thread(handle_message, core, message)
                for reply in replies:
                    await ws.send(reply)
        except GatewayError:
            await ws.close(code=1002, reason="protocol error")  # 1002 == protocol error
        except ConnectionClosed:
            pass  # client went away (possibly mid-transcribe) — a normal disconnect

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever
