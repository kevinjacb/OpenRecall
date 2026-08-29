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
from ..ingest.audio_packet import AudioPacket, PacketType
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
    Telemetry,
    TranscriptMsg,
)
from ..settings.reconciler import SLEEP_STATE
from ..sessions.index import SessionIndex
from ..sessions.lifecycle import SessionLifecycle

if TYPE_CHECKING:
    from ..contracts.types import CapabilityProvider
    from ..memory.extraction_worker import ExtractionEnqueuer
    from ..memory.speaker_registry import SpeakerRegistry
    from ..memory.store import AtomStore
    from ..sessions.segments import SegmentIndex
    from ..settings.reconciler import DeviceReconciler
    from ..settings.store import SettingsStore
    from .liveness import DeviceLiveness

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

    def drain_all(self) -> list[ProactiveMessage]:
        """Drain every pending message across all sessions and clear the wake
        event.

        This is the level-triggered drain the gateway's proactive send loop
        uses. The event is cleared *up front*, before draining, so a bare
        ``signal()`` (a wake with nothing to drain — e.g. the adapter's
        connection-close wake) does not leave the event set. If it did,
        ``wait()`` would return immediately forever without yielding and the
        drain loop would hot-spin at 100% CPU, starving the event loop — the
        'server works for ~20s then every connection times out, no error in
        the terminal' incident. Clearing first means the next ``wait()``
        blocks until a *new* ``enqueue()`` re-sets the event.

        Safe under concurrency: the proactive engine enqueues on a different
        task, but ``drain_all`` is synchronous (no ``await`` between the clear
        and the per-session drains), so no wake can be lost — a message
        enqueued after the clear re-sets the event and is drained on the next
        loop iteration.
        """
        self._event.clear()
        kept: list[ProactiveMessage] = []
        for session_id in list(self._by_session.keys()):
            kept.extend(self.drain(session_id))
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
        speaker_registry: "SpeakerRegistry | None" = None,
        atom_store: "AtomStore | None" = None,
        segment_index: "SegmentIndex | None" = None,
        reconciler: "DeviceReconciler | None" = None,
        liveness: "DeviceLiveness | None" = None,
        capability_provider: "CapabilityProvider | None" = None,
        settings: "SettingsStore | None" = None,
        command_detector=None,
        command_memory_writer=None,
    ) -> None:
        self._reconciler = reconciler
        self._liveness = liveness
        self._factory = pipeline_factory
        self._store = event_store
        self._dispatcher = dispatcher
        self._session_index = session_index
        self._segment_index = segment_index
        self._session_lifecycle = session_lifecycle
        self._enqueuer = enqueuer
        self._proactive_outbox = proactive_outbox
        self._speaker_registry = speaker_registry
        self._atom_store = atom_store
        self._caps = capability_provider
        self._settings = settings
        self._command_detector = command_detector
        self._command_memory_writer = command_memory_writer
        self._session_id: str | None = None
        self._pipeline: AudioIngestPipeline | None = None
        self._event_seq = 0  # per-session monotonic event index
        self._cum_ms = 0  # cumulative audio offset within the session
        # S5 throttle: ``request_chunks`` is emitted once per distinct open gap
        # range, not on every ``on_audio`` packet while the gap is open (which
        # spammed the control channel one request per packet). Cleared when the
        # gap closes (``missing_range()`` returns None) so a later new gap
        # emits again. Reset on hello for a fresh session.
        self._last_requested_gap: tuple[int, int] | None = None

    def on_control(self, msg: Inbound) -> list[Outbound]:
        if isinstance(msg, Hello):
            return self._on_hello(msg)
        if isinstance(msg, Bye):
            return self._on_bye(msg)
        if isinstance(msg, CommandAck):
            return self._on_command_ack(msg)
        if isinstance(msg, Telemetry):
            return self._on_telemetry(msg)
        raise GatewayError(f"unhandled control message: {msg!r}")  # pragma: no cover

    def _on_telemetry(self, msg: Telemetry) -> list[Outbound]:
        """Handle a device telemetry frame (P2 §2.5, §2.6).

        (a) Forward battery_pct/state/wake_reason to the capability provider
        so guardrails and routes read the real snapshot. A provider without
        a ``report`` seam (e.g. ``ConstantCapabilityProvider``) is a no-op,
        not an error — the capability seam stays optional.

        (b) D2: a button wake is the authoritative "turn on" — clear desired
        ``capture.sleep_mode`` so the reconciler does not immediately
        re-issue ``sleep`` against the user's explicit press. Only mutates
        if sleep_mode is currently True (avoids a spurious write and the
        reconciler churn that follows).

        Never raises on missing provider/settings — the no-op-when-None
        discipline — so a misconfigured gateway still streams. Returns no
        outbound messages: telemetry is an ingest, not a request.
        """
        if self._caps is not None:
            report = getattr(self._caps, "report", None)
            if report is not None:
                try:
                    report(
                        battery_pct=msg.battery_pct,
                        state=msg.state,
                        wake_reason=msg.wake_reason,
                    )
                except Exception:
                    logger.exception(
                        "on_telemetry_report_failed session=%s", msg.session_id,
                    )
        if msg.wake_reason == "button" and self._settings is not None:
            try:
                current = self._settings.get()
                if current.capture.sleep_mode:
                    self._settings.put(
                        current.model_copy(
                            update={
                                "capture": current.capture.model_copy(
                                    update={"sleep_mode": False},
                                ),
                            },
                        ),
                    )
                    logger.info(
                        "button_wake cleared desired sleep_mode session=%s",
                        msg.session_id,
                    )
                # A button wake means the device is now active — clear the
                # last-known sleep state so the reconciler's storm guard
                # (known_sleep is True) doesn't block a future re-sleep when
                # the user toggles sleep_mode back to True. The key matches
                # SLEEP_STATE in settings/reconciler.py.
                dev_state = self._settings.get_device_state()
                if dev_state.get(SLEEP_STATE) is True:
                    dev_state[SLEEP_STATE] = False
                    self._settings.put_device_state(dev_state)
                    logger.info(
                        "button_wake cleared last-known sleep state session=%s",
                        msg.session_id,
                    )
            except Exception:
                logger.exception(
                    "on_telemetry_clear_sleep_failed session=%s", msg.session_id,
                )
        return []

    def on_audio(self, data: bytes) -> list[Outbound]:
        if self._pipeline is None or self._session_id is None:
            # Defense-in-depth against the relay's hello/audio race (see
            # RelaySession): a binary frame can arrive just before `hello`
            # on a fresh/reconnected WebSocket. Tearing the link down for one
            # reordered frame is harsh and turns a transient race into a
            # reconnect death-loop. Drop the frame with a loud WARNING (never
            # silent) and let `hello` establish the pipeline; the live-stream
            # reassembler anchors at the first packet it does see, so a few
            # dropped head frames cost nothing on a fresh stream.
            logger.warning(
                "audio before hello — dropping %d byte frame (session not opened yet)",
                len(data),
            )
            return []

        # Every inbound audio frame passes through here, gap markers
        # included, which is what makes this a heartbeat rather than a
        # speech detector (spec §5.1). Stamped before parsing: a packet we
        # could not decode still proves the device is alive and reaching us.
        if self._liveness is not None:
            self._liveness.packet_received()
        packet = AudioPacket.parse(data)
        logger.info(
            "audio: session=%s chunk_seq=%d frames=%d vad=%s",
            self._session_id, packet.chunk_seq, len(packet.frames), packet.vad_state.name,
        )
        out: list[Outbound] = list(self._emit(self._pipeline.ingest(packet)))
        # Button short-press: the device replays its ring buffer as
        # MEMORY_CHUNK packets and flags the final one LAST_OF_REQ. Record a
        # "moment" marker event so the span is findable later (and visible in
        # the app's live feed immediately — the §E message renders as a line).
        if packet.ptype is PacketType.MEMORY_CHUNK and packet.is_last_of_request:
            out.extend(self._emit_moment())
        gap = self._pipeline.missing_range()
        if gap is not None:
            # S5: emit ``request_chunks`` once per distinct open gap range.
            # Without this throttle every ``on_audio`` packet while the gap is
            # open appended a fresh ``RequestChunks`` — one per inbound packet
            # — which spammed the control channel and the relay's request
            # queue. Track the last-requested ``(start, end)`` and only
            # re-emit when the gap range actually changes (e.g. a partial
            # backfill shrinks it, or a re-anchor shifts it).
            key = (gap[0], gap[1])
            if self._last_requested_gap != key:
                logger.info("request_chunks: session=%s gap=[%d, %d)", self._session_id, gap[0], gap[1])
                out.append(RequestChunks(session_id=self._session_id, start=gap[0], end=gap[1]))
                self._last_requested_gap = key
        else:
            # Gap closed — clear the throttle so a later new gap emits again.
            self._last_requested_gap = None
        out.append(Ack(session_id=self._session_id, next_seq=self._pipeline.next_expected_seq))
        return out

    async def send_proactive(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        atoms: tuple[str, ...],
        propose: dict | None = None,
    ) -> None:
        """WsSender seam (P3). The engine calls this to push a proactive
        answer onto the open WebSocket. We enqueue the message into the
        per-session outbox; the adapter's proactive task wakes up and
        drains it as a ``proactive`` §E frame.

        ``propose`` carries an optional inline proposal (e.g. a
        ``{kind:"name_speaker", speaker_id}`` nudge) so the phone can
        render a quick input instead of a free-form reply.

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
                propose=propose,
            ),
        )
        self._proactive_outbox.signal()

    def _on_hello(self, msg: Hello) -> list[Outbound]:
        self._session_id = msg.session_id
        self._last_requested_gap = None  # fresh session — reset the S5 throttle
        self._pipeline = self._factory(msg.start_seq)
        # The factory only knows start_seq; the session id arrives here, and
        # the audio log is keyed by it (spec §3.1).
        bind = getattr(self._pipeline, "set_audio_target", None)
        if bind is not None:
            bind(msg.session_id)
        # Continue the per-session event counter past any events already in the
        # store for this session. The BLE relay does NOT replay on reconnect —
        # it resumes at the device's current chunk_seq (a boot-relative
        # monotonic that never resets) — so the audio after a reconnect is NEW
        # content. Resetting _event_seq to 0 made every post-reconnect
        # transcript collide with an existing "{session}:{seq}" event_id and
        # be dropped as a duplicate (stored=False), so it was never enqueued
        # for extraction and never became a memory (the 'chat says no memory
        # after reconnect' incident). For a genuinely new session (no prior
        # events) last_event returns None and we start at 0 as before.
        prior = self._store.last_event(msg.session_id) if self._store is not None else None
        if prior is not None:
            self._event_seq = prior.seq + 1
            self._cum_ms = prior.start_ms + prior.duration_ms
            logger.info(
                "hello: session=%s start_seq=%d resuming event_seq=%d cum_ms=%d",
                msg.session_id, msg.start_seq, self._event_seq, self._cum_ms,
            )
        else:
            self._event_seq = 0
            self._cum_ms = 0
            logger.info("hello: session=%s start_seq=%d (fresh)", msg.session_id, msg.start_seq)
        if self._session_lifecycle is not None:
            self._session_lifecycle.register(msg.session_id)
        # The device just became reachable, which is the moment to converge
        # it on the settings the user chose while it was offline (spec §4.2).
        # Issued before _pending_commands so a freshly-issued reconcile
        # command rides out on this same hello rather than waiting for the
        # next ack round trip.
        if self._reconciler is not None:
            try:
                self._reconciler.reconcile()
            except Exception:
                # Reconciliation is a convergence mechanism, not a
                # precondition for streaming. A failure must not refuse the
                # connection; the next hello retries.
                logger.exception("reconcile_on_hello_failed session=%s", msg.session_id)
        # initial sync: ack the cursor, then hand over any commands awaiting this session
        return [Ack(session_id=msg.session_id, next_seq=msg.start_seq), *self._pending_commands()]

    def _on_command_ack(self, msg: CommandAck) -> list[Outbound]:
        if self._dispatcher is None:
            raise GatewayError("command_ack received but no dispatcher is configured")
        # Capture the type before acking: the ack is the only evidence the
        # device actually changed state, and it is what stops the reconciler
        # re-issuing on every reconnect (spec §4.2).
        acked_type = self._command_type(msg.command_id)
        self._dispatcher.ack(msg.command_id)
        if self._reconciler is not None and acked_type is not None:
            try:
                self._reconciler.note_acked(acked_type)
            except Exception:
                logger.exception("reconcile_note_ack_failed command=%s", msg.command_id)
        # --- command memory on ack (speech→command channel) ---
        # Reuse acked_type (captured BEFORE the ack at the top of this method):
        # after ack() the command moves out of pending() and _command_type would
        # return None, so the memory writer would never fire if re-called here.
        # Voice-command provenance (source_event_id) comes from the detector's
        # _provenance map; HTTP /agent-issued commands have no provenance and are
        # skipped (source_event_id is None). None-defaulted for back-compat.
        if self._command_memory_writer is not None and acked_type is not None:
            source_event_id = None
            if self._command_detector is not None:
                source_event_id = self._command_detector._provenance.get(msg.command_id)
            if source_event_id is not None:
                self._command_memory_writer.on_ack(
                    command_id=msg.command_id, session_id=self._session_id or "",
                    command_type=acked_type, source_event_id=source_event_id,
                    trigger_text=None,
                )
        return list(self._pending_commands())  # pull the remainder

    def _command_type(self, command_id: str) -> str | None:
        for signed in self._dispatcher.pending():
            if signed.command.command_id == command_id:
                return signed.command.type
        return None

    def _pending_commands(self) -> list[CommandMessage]:
        """Signed commands awaiting this session, as §E command messages.

        A command with an **empty** ``session_id`` is *unbound*: "the device,
        whenever it is next connected" (spec D3). HTTP callers can only issue
        unbound commands, because session ids are relay-minted UUIDs that are
        never surfaced over HTTP — so before this, a Settings toggle had no
        correct value to target and its command was undeliverable.

        The live session id is stamped into the outgoing message here. The
        *signed* payload keeps the empty value, so the signature still
        verifies — safe because the firmware never reads ``session_id``.
        """
        if self._dispatcher is None or self._session_id is None:
            return []
        return [
            CommandMessage(session_id=self._session_id, **signed.to_wire())
            for signed in self._dispatcher.pending()
            if signed.command.session_id in ("", self._session_id)
        ]

    def _on_bye(self, msg: Bye) -> list[Outbound]:
        if self._pipeline is None or self._session_id is None:
            raise GatewayError("bye received before hello")
        flushed = list(self._emit(self._pipeline.flush()))
        logger.info("bye: session=%s flushed %d final transcript(s)", msg.session_id, len(flushed))
        closing_session = self._session_id
        self._close_session(closing_session)
        # Bug C: the live extraction path held the trailing still-growing 60s
        # window back (finalize=False). The session ending is the signal to
        # finalize it — enqueue a finalize=True pass so the trailing partial
        # window forms memories without waiting for a server restart. Done
        # before clearing _session_id so the bye itself owns the finalize and
        # the disconnect path's finalize_pending_session() is a no-op after.
        if self._enqueuer is not None:
            self._enqueuer.enqueue_finalize(closing_session)
            logger.info("enqueued finalize (bye) for session=%s", closing_session)
        self._session_id = None
        self._pipeline = None
        return flushed

    def _close_session(self, session_id: str) -> None:
        """Release a session's connection-scoped state (spec §0.1, §0.2).

        Deregisters it from the lifecycle registry (so ``activeSessions``
        deflates and §5.2's "409 while open" does not make the session
        permanently undeletable) and stamps ``ended_at`` on its summary.
        Both are idempotent, which is what lets ``bye`` and the transport's
        ``finally`` block both call this.
        """
        if self._session_lifecycle is not None:
            self._session_lifecycle.deregister(session_id)
        if self._session_index is not None:
            self._session_index.mark_closed(session_id, datetime.now(timezone.utc))
        # Best-effort accelerator only: the idle sweep closes the same
        # segment moments later, which is what keeps §2.1 correct when the
        # connection dies without a bye.
        if self._segment_index is not None:
            self._segment_index.close_session(session_id)

    def on_disconnect(self) -> None:
        """Everything the transport must do when a connection ends.

        ``bye`` is unreliable (spec §0.3): on a WebSocket drop the relay
        writes it into an already-dead socket, so the server usually never
        sees it. The transport's ``finally`` block is therefore the only
        close signal that always fires, and it owns the full teardown —
        finalize the trailing extraction window, deregister the session,
        and close its summary.

        A no-op for a session that already said ``bye`` (which cleared
        ``_session_id``) or never said ``hello``.
        """
        # Flush any pending transcription (e.g. a sentence held by the
        # sentence coalescer) so a dropped link does not lose the last
        # sentence. ``bye`` flushes on its own path and then clears the
        # pipeline, so this only acts when bye never arrived — the common
        # case for a wearable whose WebSocket just dropped. The final
        # transcripts are persisted as events; the WS send is best-effort
        # (the socket is already dead), so the value is keeping the last
        # sentence in the recording + extraction, not reaching the phone.
        if self._pipeline is not None and self._session_id is not None:
            try:
                flushed = list(self._emit(self._pipeline.flush()))
                if flushed:
                    logger.info(
                        "disconnect: flushed %d final transcript(s) for "
                        "session=%s", len(flushed), self._session_id,
                    )
            except Exception:
                logger.exception(
                    "disconnect: final flush failed for session=%s",
                    self._session_id,
                )
        self.finalize_pending_session()
        if self._session_id is not None:
            self._close_session(self._session_id)

    def finalize_pending_session(self) -> None:
        """Finalize the trailing extraction window of the still-open session.

        Bug C: a relay disconnect without a clean ``bye`` leaves the live
        extraction cursor holding the trailing partial 60s window back
        (finalize=False on the live path). Without a finalize, those last
        ~up-to-60s of speech never form memories until a server restart
        re-runs reconcile-on-start. The WebSocket adapter calls this from
        the connection ``finally`` block so a dropped link finalizes the
        session it was carrying.

        A no-op when there is no open session (never hello'd, or already
        bye'd — bye enqueues its own finalize and clears ``_session_id``),
        so the disconnect path does not double-finalize a cleanly-closed
        session.
        """
        if self._session_id is None or self._enqueuer is None:
            return
        self._enqueuer.enqueue_finalize(self._session_id)
        logger.info(
            "enqueued finalize (disconnect) for session=%s", self._session_id,
        )

    _MOMENT_TEXT = "\U0001F4CD Moment marked — the last minute was saved"

    def _emit_moment(self) -> list[TranscriptMsg]:
        """Record a button-press moment marker as a §F event + §E message.

        Deliberately NOT routed through :meth:`_emit`: a moment is not
        speech, so it must not be enqueued for memory extraction or fed to
        the command detector (and the extraction worker filters
        non-transcript kinds as defense in depth). The session/segment
        indexes receive it and ignore non-transcript kinds themselves.
        """
        if self._session_id is None:
            return []
        event = CaptureEvent(
            event_id=f"{self._session_id}:{self._event_seq}",
            session_id=self._session_id,
            seq=self._event_seq,
            kind="moment",
            created_at=datetime.now(timezone.utc),
            text=self._MOMENT_TEXT,
            duration_ms=0,
            start_ms=self._cum_ms,
        )
        stored = True
        if self._store is not None:
            stored = self._store.append(event)
        if stored and self._session_index is not None:
            self._session_index.record(event)
        if stored and self._segment_index is not None:
            self._segment_index.record(event)
        self._event_seq += 1
        logger.info(
            "moment: session=%s event=%s (button replay complete)",
            self._session_id, event.event_id,
        )
        return [TranscriptMsg(
            session_id=self._session_id, text=self._MOMENT_TEXT, duration_ms=0,
        )]

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
                speaker=t.speaker,
                speaker_confidence=t.speaker_confidence,
                speaker_assignment=t.speaker_assignment,
            )
            stored = True
            if self._store is not None:
                stored = self._store.append(event)
            if stored and self._session_index is not None:
                self._session_index.record(event)
            # Segments are cut from the same event stream (spec §2.1). Fed
            # here rather than from a separate consumer so the index the app
            # reads can never lag the events it is derived from.
            if stored and self._segment_index is not None:
                self._segment_index.record(event)
            if stored and self._liveness is not None:
                self._liveness.transcript_emitted()
            # M4.3 wiring: every successful event append enqueues the
            # session for the background ExtractionWorker. The enqueuer
            # is bounded and dedupes, so this is safe per-transcript.
            # Without this seam the worker loop sat idle forever, the
            # atom table stayed empty, and POST /agent always refused
            # with NO_SUPPORTING_MEMORY. None-defaulted for back-compat
            # with tests that don't wire a worker.
            if stored and self._enqueuer is not None and self._session_id is not None:
                self._enqueuer.enqueue(self._session_id)
                logger.debug(
                    "enqueued for extraction session=%s event=%s seq=%d",
                    self._session_id, event.event_id, event.seq,
                )
            # Speech→command channel (Stage 1): feed every stored transcript
            # event to the command detector so it can match command phrases
            # per-sentence (immediate, NOT on a 60s extraction window). None-
            # defaulted so existing callers/tests are unchanged.
            if stored and self._command_detector is not None and self._session_id is not None:
                self._command_detector.feed(self._session_id, event)
            self._event_seq += 1
            self._cum_ms += t.duration_ms
            logger.info(
                "transcript: session=%s event=%s text=%r (%d ms) stored=%s",
                self._session_id, event.event_id, t.text, t.duration_ms, stored,
            )
            # Resolve the display name + wearer flag at emit time from the
            # registry so renames/reassigns reflect immediately in history
            # without a backfill. None/False when the hop has no speaker or the
            # registry has no row (e.g. speaker recognition disabled).
            sp = (
                self._speaker_registry.get(t.speaker)
                if (t.speaker and self._speaker_registry is not None)
                else None
            )
            msgs.append(
                TranscriptMsg(
                    session_id=self._session_id, text=t.text, duration_ms=t.duration_ms,
                    speaker=t.speaker, speaker_confidence=t.speaker_confidence,
                    speaker_assignment=t.speaker_assignment,
                    speaker_name=sp.display_name if sp is not None else None,
                    is_wearer=sp.is_wearer if sp is not None else False,
                )
            )
        return msgs
