"""Transport-agnostic gateway core: the per-connection session state machine.

One :class:`GatewayCore` instance serves one BLE-relayed WebSocket connection
(XIAO -> Android -> Mac). It consumes already-deframed inbound messages — JSON
control (§E) and binary §C.6 audio — and returns the §E messages to send back. It
holds no socket and does no I/O, so the entire protocol is unit-testable; the
websockets adapter is a thin shell that only moves bytes and calls these methods.

Per packet it drives three things off the pipeline/reassembler:
  * any completed transcription windows -> ``transcript`` messages,
  * a contiguous head gap -> a ``request_chunks`` backfill request,
  * the cursor -> an ``ack`` of the next contiguous chunk_seq wanted.

P3 also exposes :meth:`GatewayCore.send_proactive` as the
:class:`WsSender` seam for the :class:`ProactiveTriggerEngine`. Proactive
frames ride the same open WebSocket as a ``proactive`` §E message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Union

from ..agent.metrics import InMemoryMetricsRecorder
from ..commands.dispatcher import CommandDispatcher
from ..contracts.clock import Clock
from ..contracts.metrics import Metrics
from ..events.model import CaptureEvent
from ..events.store import EventStore
from ..ingest.audio_packet import AudioPacket
from ..ingest.pipeline import AudioIngestPipeline, Transcript
from ..protocol.messages import (
    Ack,
    Bye,
    CommandAck,
    CommandMessage,
    Hello,
    Inbound,
    ProactiveMessage,
    RequestChunks,
    TranscriptMsg,
)
from ..sessions.index import SessionIndex
from ..sessions.lifecycle import SessionLifecycle

if TYPE_CHECKING:
    from ..memory.extraction_worker import ExtractionEnqueuer

Outbound = Union[Ack, RequestChunks, TranscriptMsg, CommandMessage, ProactiveMessage]
PipelineFactory = Callable[[int], AudioIngestPipeline]

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """A protocol violation by the (untrusted) client — the transport should close."""


class ProactiveOutbox:
    """Per-session queue of pending proactive messages (P3).

    Best-effort delivery: messages older than ``ttl_s`` are dropped at
    drain time (and counted as
    ``PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=ttl_exceeded}``). The
    outbox is in-process only — a server restart drops everything
    in flight, which is acceptable given the metric.
    """

    def __init__(
        self,
        ttl_s: float = 30.0,
        clock: Clock | None = None,
        metrics: InMemoryMetricsRecorder | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._metrics = metrics
        # session_id -> list[(enqueued_at, msg)]
        self._by_session: dict[str, list[tuple[datetime, ProactiveMessage]]] = {}
        self._event = asyncio.Event()

    def enqueue(self, msg: ProactiveMessage) -> None:
        if self._clock is not None:
            now = self._clock.now()
        else:
            now = datetime.now(tz=timezone.utc)
        self._by_session.setdefault(msg.session_id, []).append((now, msg))
        self._event.set()

    def drain(self, session_id: str) -> list[ProactiveMessage]:
        if self._clock is not None:
            now = self._clock.now()
        else:
            now = datetime.now(tz=timezone.utc)
        entries = self._by_session.pop(session_id, [])
        kept: list[ProactiveMessage] = []
        for enq_at, m in entries:
            if (now - enq_at).total_seconds() > self._ttl_s:
                if self._metrics is not None:
                    self._metrics.increment(
                        Metrics.PROACTIVE_DELIVERY_DROPPED_TOTAL,
                        tags={"reason": "ttl_exceeded"},
                    )
                continue
            kept.append(m)
        if not self._by_session:
            self._event.clear()
        return kept

    async def wait(self) -> None:
        await self._event.wait()

    def signal(self) -> None:
        self._event.set()


class GatewayCore:
    def __init__(
        self,
        pipeline_factory: PipelineFactory,
        event_store: EventStore | None = None,
        dispatcher: CommandDispatcher | None = None,
        session_index: SessionIndex | None = None,
        session_lifecycle: SessionLifecycle | None = None,
        enqueuer: "ExtractionEnqueuer | None" = None,
        proactive_outbox: ProactiveOutbox | None = None,
    ) -> None:
        self._factory = pipeline_factory
        self._store = event_store
        self._dispatcher = dispatcher
        self._session_index = session_index
        self._session_lifecycle = session_lifecycle
        self._enqueuer = enqueuer
        self._proactive_outbox = proactive_outbox
        self._session_id: str | None = None
        self._pipeline: AudioIngestPipeline | None = None
        self._event_seq = 0  # per-session monotonic event index
        self._cum_ms = 0  # cumulative audio offset within the session

    def on_control(self, msg: Inbound) -> list[Outbound]:
        if isinstance(msg, Hello):
            return self._on_hello(msg)
        if isinstance(msg, Bye):
            return self._on_bye(msg)
        if isinstance(msg, CommandAck):
            return self._on_command_ack(msg)
        raise GatewayError(f"unhandled control message: {msg!r}")  # pragma: no cover

    def on_audio(self, data: bytes) -> list[Outbound]:
        if self._pipeline is None or self._session_id is None:
            raise GatewayError("audio received before hello")

        packet = AudioPacket.parse(data)
        logger.info(
            "audio: session=%s chunk_seq=%d frames=%d vad=%s",
            self._session_id, packet.chunk_seq, len(packet.frames), packet.vad_state.name,
        )
        out: list[Outbound] = list(self._emit(self._pipeline.ingest(packet)))
        gap = self._pipeline.missing_range()
        if gap is not None:
            logger.info("request_chunks: session=%s gap=[%d, %d)", self._session_id, gap[0], gap[1])
            out.append(RequestChunks(session_id=self._session_id, start=gap[0], end=gap[1]))
        out.append(Ack(session_id=self._session_id, next_seq=self._pipeline.next_expected_seq))
        return out

    async def send_proactive(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        atoms: tuple[str, ...],
    ) -> None:
        """WsSender seam (P3). The engine calls this to push a proactive
        answer onto the open WebSocket. We enqueue the message into the
        per-session outbox; the adapter's proactive task wakes up and
        drains it as a ``proactive`` §E frame.

        Raises if no outbox is wired (refusing to silently drop is the
        whole point of a WsSender)."""
        if self._proactive_outbox is None:
            raise RuntimeError(
                "send_proactive called but GatewayCore has no ProactiveOutbox; "
                "wire one in __init__",
            )
        self._proactive_outbox.enqueue(
            ProactiveMessage(
                session_id=session_id,
                request_id=request_id,
                text=text,
                atoms=atoms,
            ),
        )
        self._proactive_outbox.signal()

    def _on_hello(self, msg: Hello) -> list[Outbound]:
        self._session_id = msg.session_id
        self._pipeline = self._factory(msg.start_seq)
        self._event_seq = 0
        self._cum_ms = 0
        if self._session_lifecycle is not None:
            self._session_lifecycle.register(msg.session_id)
        logger.info("hello: session=%s start_seq=%d", msg.session_id, msg.start_seq)
        # initial sync: ack the cursor, then hand over any commands awaiting this session
        return [Ack(session_id=msg.session_id, next_seq=msg.start_seq), *self._pending_commands()]

    def _on_command_ack(self, msg: CommandAck) -> list[Outbound]:
        if self._dispatcher is None:
            raise GatewayError("command_ack received but no dispatcher is configured")
        self._dispatcher.ack(msg.command_id)
        return list(self._pending_commands())  # pull the remainder

    def _pending_commands(self) -> list[CommandMessage]:
        """Signed commands still awaiting *this* session, as §E command messages."""
        if self._dispatcher is None or self._session_id is None:
            return []
        return [
            CommandMessage(session_id=self._session_id, **signed.to_wire())
            for signed in self._dispatcher.pending()
            if signed.command.session_id == self._session_id
        ]

    def _on_bye(self, msg: Bye) -> list[Outbound]:
        if self._pipeline is None or self._session_id is None:
            raise GatewayError("bye received before hello")
        flushed = list(self._emit(self._pipeline.flush()))
        logger.info("bye: session=%s flushed %d final transcript(s)", msg.session_id, len(flushed))
        closing_session = self._session_id
        if self._session_lifecycle is not None:
            self._session_lifecycle.deregister(closing_session)
        self._session_id = None
        self._pipeline = None
        return flushed

    def _emit(self, transcripts: list[Transcript]) -> list[TranscriptMsg]:
        """Persist each transcript as a §F capture event and build its §E message.

        Each event gets a per-session monotonic ``seq`` and a cumulative ``start_ms``
        offset; ``event_id`` is ``"{session}:{seq}"`` so an at-least-once session
        replay regenerates identical ids and the store dedupes them.

        On a successful (non-duplicate) append, the event is also folded into the
        :class:`SessionIndex` so the HTTP ``/sessions`` route sees a live view
        of the session.
        """
        assert self._session_id is not None  # only called while bound
        msgs: list[TranscriptMsg] = []
        for t in transcripts:
            event = CaptureEvent(
                event_id=f"{self._session_id}:{self._event_seq}",
                session_id=self._session_id,
                seq=self._event_seq,
                kind="transcript",
                created_at=datetime.now(timezone.utc),
                text=t.text,
                duration_ms=t.duration_ms,
                start_ms=self._cum_ms,
            )
            stored = True
            if self._store is not None:
                stored = self._store.append(event)
            if stored and self._session_index is not None:
                self._session_index.record(event)
            # M4.3 wiring: every successful event append enqueues the
            # session for the background ExtractionWorker. The enqueuer
            # is bounded and dedupes, so this is safe per-transcript.
            # Without this seam the worker loop sat idle forever, the
            # atom table stayed empty, and POST /agent always refused
            # with NO_SUPPORTING_MEMORY. None-defaulted for back-compat
            # with tests that don't wire a worker.
            if stored and self._enqueuer is not None and self._session_id is not None:
                self._enqueuer.enqueue(self._session_id)
            self._event_seq += 1
            self._cum_ms += t.duration_ms
            logger.info(
                "transcript: session=%s event=%s text=%r (%d ms) stored=%s",
                self._session_id, event.event_id, t.text, t.duration_ms, stored,
            )
            msgs.append(
                TranscriptMsg(
                    session_id=self._session_id, text=t.text, duration_ms=t.duration_ms
                )
            )
        return msgs
