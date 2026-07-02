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
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Union

from ..commands.dispatcher import CommandDispatcher
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
    RequestChunks,
    TranscriptMsg,
)

Outbound = Union[Ack, RequestChunks, TranscriptMsg, CommandMessage]
PipelineFactory = Callable[[int], AudioIngestPipeline]


class GatewayError(Exception):
    """A protocol violation by the (untrusted) client — the transport should close."""


class GatewayCore:
    def __init__(
        self,
        pipeline_factory: PipelineFactory,
        event_store: EventStore | None = None,
        dispatcher: CommandDispatcher | None = None,
    ) -> None:
        self._factory = pipeline_factory
        self._store = event_store
        self._dispatcher = dispatcher
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
        out: list[Outbound] = list(self._emit(self._pipeline.ingest(packet)))
        gap = self._pipeline.missing_range()
        if gap is not None:
            out.append(RequestChunks(session_id=self._session_id, start=gap[0], end=gap[1]))
        out.append(Ack(session_id=self._session_id, next_seq=self._pipeline.next_expected_seq))
        return out

    def _on_hello(self, msg: Hello) -> list[Outbound]:
        self._session_id = msg.session_id
        self._pipeline = self._factory(msg.start_seq)
        self._event_seq = 0
        self._cum_ms = 0
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
        self._session_id = None
        self._pipeline = None
        return flushed

    def _emit(self, transcripts: list[Transcript]) -> list[TranscriptMsg]:
        """Persist each transcript as a §F capture event and build its §E message.

        Each event gets a per-session monotonic ``seq`` and a cumulative ``start_ms``
        offset; ``event_id`` is ``"{session}:{seq}"`` so an at-least-once session
        replay regenerates identical ids and the store dedupes them.
        """
        assert self._session_id is not None  # only called while bound
        msgs: list[TranscriptMsg] = []
        for t in transcripts:
            if self._store is not None:
                self._store.append(
                    CaptureEvent(
                        event_id=f"{self._session_id}:{self._event_seq}",
                        session_id=self._session_id,
                        seq=self._event_seq,
                        kind="transcript",
                        created_at=datetime.now(timezone.utc),
                        text=t.text,
                        duration_ms=t.duration_ms,
                        start_ms=self._cum_ms,
                    )
                )
            self._event_seq += 1
            self._cum_ms += t.duration_ms
            msgs.append(
                TranscriptMsg(
                    session_id=self._session_id, text=t.text, duration_ms=t.duration_ms
                )
            )
        return msgs
