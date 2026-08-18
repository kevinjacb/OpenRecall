#!/usr/bin/env python3
"""Run the OpenRecall gateway WebSocket server + HTTP control API.

This is the live entry point the Android relay connects to (XIAO -> Android -> Mac).
It wires the real Opus decoder + MLX-whisper transcriber per session, so it needs
the heavy extras installed on the Mac:

    pip install -e '.[mlx,opus]'   # also: brew install opus
    python scripts/run_gateway.py --port 8765 --window-ms 5000

Two servers share one event loop:

  * WebSocket gateway (``--port``, default 8765) — the BLE-relayed audio + control
    plane. §E text frames carry control JSON; §C.6 binary frames carry audio.
  * HTTP control API (``--http-port``, default 8766) — operator/phone-facing
    endpoints (``/health``, ``/provisioning/pubkey``), guarded by a bearer token.

On startup it prints, in order: the HTTP control URL, the WS URL, the bearer token
(copy to the phone), and the server command-signing public key (provision on device).

Protocol (one WS connection == one session):
  * text frame:  §E JSON control, e.g. {"type":"hello","session_id":"...","start_seq":0}
  * binary frame: a §C.6 audio packet
  * server replies (text JSON): ack / request_chunks / transcript
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import aiohttp.web

from openrecall_server.agent.metrics import InMemoryMetricsRecorder
from openrecall_server.auth import load_or_create_token
from openrecall_server.commands.dispatcher import CommandDispatcher
from openrecall_server.commands.signing import load_or_create_signer
from openrecall_server.contracts.clock import SystemClock
from openrecall_server.events.store import SqliteEventStore
from openrecall_server.gateway.adapter import build_pipeline_factory, serve
from openrecall_server.gateway.core import ProactiveOutbox
from openrecall_server.http.app import build_app
from openrecall_server.media.audio import AudioStore
from openrecall_server.media.retention import RetentionSweeper
from openrecall_server.memory.atom import MemoryAtom  # noqa: F401  (used in stage wiring)
from openrecall_server.memory.backfill import backfill_occurred_at
from openrecall_server.memory.embeddings import OpenAICompatibleEmbedder
from openrecall_server.memory.extract import LLMExtractor
from openrecall_server.memory.llm import OpenAICompatibleChatModel
from openrecall_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
)
from openrecall_server.memory.index import SqliteMemoryIndex
from openrecall_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from openrecall_server.memory.store import SqliteAtomStore
from openrecall_server.gateway.liveness import DeviceLiveness
from openrecall_server.sessions.index import SessionIndex
from openrecall_server.sessions.lifecycle import SessionLifecycle
from openrecall_server.sessions.segment_meta import SqliteSegmentMetaStore
from openrecall_server.sessions.segments import SegmentIndex
from openrecall_server.sessions.sweeper import SegmentSweeper
from openrecall_server.sessions.titler import SegmentTitler
from openrecall_server.settings.reconciler import DeviceReconciler
from openrecall_server.settings.store import SqliteSettingsStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--window-ms", type=int, default=5000, help="transcription window")
    ap.add_argument("--model", default=None, help="override the MLX-whisper model repo")
    ap.add_argument("--db", default="data/events.db", help="durable capture-event store path")
    ap.add_argument("--key-file", default="data/server_ed25519.key",
                    help="server command-signing key (created on first run)")
    ap.add_argument("--token-file", default="data/server_token",
                    help="bearer token file for the HTTP control API + WS auth (created on first run)")
    ap.add_argument("--http-port", type=int, default=8766,
                    help="HTTP control API port (operator/phone-facing)")
    args = ap.parse_args()

    # Bring-up observability: INFO shows the full relay + memory + proactive
    # flow (connection, hello, transcripts, event append, extraction enqueue,
    # windowing, LLM calls, cursor advance, proactive triggers). DEBUG adds
    # per-frame / per-event detail. Override with OPENRECALL_LOG_LEVEL (e.g.
    # OPENRECALL_LOG_LEVEL=WARNING to quiet it once stable, =DEBUG for everything).
    # Format includes time + logger name so the source is obvious.
    _log_level = __import__("os").environ.get("OPENRECALL_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, _log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(args.db)
    signer = load_or_create_signer(args.key_file)
    from openrecall_server.commands.store import SqliteCommandStore
    command_store = SqliteCommandStore(
        args.db.replace("events.db", "commands.db")
    )
    dispatcher = CommandDispatcher(signer, store=command_store)
    # NOTE: the pipeline factory is built later (after `agent_config`
    # is loaded) so it can thread the whisper noise-filter thresholds
    # through to the streaming transcriber.

    # Phase 3 dependencies: the index backs `/sessions`, the lifecycle backs
    # `/status`'s active_sessions counter. Both are process-wide and in-memory.
    # Cold start: rebuild the index from the durable event store so a
    # gateway restart does not 404 every session that was captured before
    # the restart. The live record() path keeps the index in sync after
    # the rebuild; the two are independent and safe to interleave under
    # SessionIndex's existing lock.
    session_index = SessionIndex()
    session_index.rebuild_from_store(store)
    session_lifecycle = SessionLifecycle()

    # Segments (spec §2.1): the derived "recording" the app actually lists.
    # Same cold-start story as the session index, and safe for the same
    # reason — segment ids are deterministic, so the durable titles keyed by
    # them survive the rebuild.
    segment_index = SegmentIndex()
    segment_index.rebuild_from_store(store)
    segment_meta = SqliteSegmentMetaStore(
        args.db.replace("events.db", "segment_meta.db")
    )
    # Device liveness (spec §5.1): the connection-freshness signal behind
    # /device/status. Written by the gateway, read by HTTP.
    liveness = DeviceLiveness()
    # Audio plane (spec §3.1): a per-session Opus frame log next to the
    # databases. Raw frames on write; the Ogg container is built on read.
    audio_store = AudioStore(Path(args.db).parent / "audio")
    # Settings are durable desired state (spec D2): a command-only toggle is
    # dead whenever the device is offline, which is most of the time.
    settings_store = SqliteSettingsStore(
        args.db.replace("events.db", "settings.db")
    )

    # Memory pipeline (M4.4 wiring): the gateway offloads extraction +
    # embedding + indexing to an out-of-band worker so the audio hot path
    # stays real-time. The enqueuer is the only seam the gateway hot path
    # touches; the worker drains it on a background task. The metrics
    # recorder is process-wide so /metrics reflects the same numbers.
    metrics = InMemoryMetricsRecorder()
    atom_store = SqliteAtomStore(args.db.replace("events.db", "atoms.db"))
    # P1 instruction processor: reminders side table + recent-transcript
    # provider. The reminder store is a separate DB file (twin to atoms.db);
    # the recent-transcript provider reads the event store the gateway
    # already owns.
    from openrecall_server.reminders.store import SqliteReminderStore
    reminder_store = SqliteReminderStore(args.db.replace("events.db", "reminders.db"))
    from openrecall_server.agent.context import EventBackedRecentTranscript
    recent_transcript = EventBackedRecentTranscript(store)
    # Spec §1.1: give historical atoms their real conversation time, joined
    # from the source capture event. Idempotent and best-effort — a row that
    # can't be resolved keeps the created_at fallback.
    backfill_occurred_at(store, atom_store)
    memory_index = SqliteMemoryIndex(args.db.replace("events.db", "memory_index.db"))
    # Speaker recognition: a Sqlite registry + an identifier wired into the
    # pipeline only when OPENRECALL_SPEAKER_ENABLED=true. Off by default — when
    # disabled, build_speaker_identifier returns None and the pipeline wires
    # no identifier, so zero embed calls run and every Transcript carries
    # speaker=None.
    from openrecall_server.gateway.adapter import build_speaker_identifier
    from openrecall_server.ingest.speaker_config import load_speaker_config
    from openrecall_server.memory.speaker_registry import SqliteSpeakerRegistry

    speaker_cfg = load_speaker_config(__import__("os").environ)
    speaker_registry = SqliteSpeakerRegistry(
        args.db.replace("events.db", "speakers.db"), speaker_cfg,
    )
    speaker_identifier = build_speaker_identifier(
        speaker_cfg, speaker_registry, embedder=None,
    )
    # Warm up the real embedder at startup so the first connection pays no
    # model-init cost. No-op for the fake embedder / when disabled. Best-effort:
    # a warmup failure must not abort the gateway.
    if speaker_identifier is not None:
        try:
            speaker_identifier.warmup_embedder()
        except Exception:
            logging.exception("speaker_warmup_failed")
    embedder = OpenAICompatibleEmbedder.from_env(__import__("os").environ)
    llm_chat = OpenAICompatibleChatModel.from_env(__import__("os").environ)
    extractor = LLMExtractor(llm_chat)
    # EXTRACTOR_VERSION: identity of the extraction algorithm + prompt. The
    # worker stamps the per-session cursor with this; bumping it invalidates
    # every existing cursor so the new extractor re-extracts historical
    # transcripts instead of silently skipping them. Bump when you change the
    # windowing, the prompt in memory/extract.py, or the extractor model in a
    # way that should re-form memories. Legacy cursors (stamped 'legacy' by
    # pre-versioning code / the old per-event pipeline) mismatch this and are
    # re-extracted automatically on the next worker start.
    EXTRACTOR_VERSION = "v1"
    pipeline = Pipeline(
        extraction=ExtractionStage(
            extractor=extractor,
            clock=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            version=EXTRACTOR_VERSION,
        ),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=embedder),
        indexing=IndexingStage(index=memory_index),
        store=atom_store,
    )
    # Segment close is clock-driven, not event-driven (spec §2.1) — "no
    # transcript for five minutes" cannot be observed by waiting for the next
    # transcript. The sweep also owns titling, because "just closed" is when
    # a segment is both complete and worth naming.
    segment_sweeper = SegmentSweeper(
        index=segment_index,
        titler=SegmentTitler(events=store, meta=segment_meta, llm_chat=llm_chat),
    )
    enqueuer = ExtractionEnqueuer(capacity=1024, metrics=metrics)
    worker = ExtractionWorker(
        events=store,
        atoms=atom_store,
        pipeline=pipeline,
        metrics=metrics,
        enqueuer=enqueuer,
    )

    # P3 proactive: a single process-wide ProactiveOutbox shared by every
    # WebSocket connection's proactive_task. The engine listens on the
    # worker for SessionCompletion, plans, and forwards RETURN results
    # to whichever GatewayCore is the active ws_sender at the moment.
    proactive_outbox = ProactiveOutbox(ttl_s=30.0, clock=SystemClock(), metrics=metrics)

    # Cognitive read path (N3.4 wiring): construct the Planner with the
    # real components — async LLM, strict validator, confidence-gated
    # guardrails, durable audit log, and a constant capability stub.
    # The retriever and atoms store are the same ones the worker
    # populates, so a freshly-extracted atom is immediately queryable.
    from openrecall_server.agent.audit import InMemoryAuditLogger
    from openrecall_server.agent.capability import ConstantCapabilityProvider
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.agent.context import ContextBuilder
    from openrecall_server.agent.guardrails import ConfidenceGateGuardrails
    from openrecall_server.agent.guardrails_command import StrictCommandGuardrails
    from openrecall_server.agent.intent import OpenAICompatibleAgentLLM
    from openrecall_server.agent.planner import Planner
    from openrecall_server.agent.validator import StrictJSONValidator
    from openrecall_server.agent.validator_command import StrictCommandValidator
    from openrecall_server.contracts.id_generator import UuidIdGenerator
    from openrecall_server.memory.retrieval import Retriever
    from openrecall_server.memory.scoring import SimRecencyScorer

    agent_llm = OpenAICompatibleAgentLLM(llm_chat)
    # Read OPENRECALL_CONFIDENCE_AUTONOMOUS / OPENRECALL_CONFIDENCE_CONFIRM /
    # OPENRECALL_RATE_LIMIT_PER_MIN from the process environment. Defaults match
    # the spec; a bad value aborts startup with a clear error.
    agent_config = load_agent_config(__import__("os").environ)
    planner = Planner(
        retriever=Retriever(
            embedder=embedder,
            index=memory_index,
            scorer=SimRecencyScorer(),
            clock=SystemClock(),
            ids=UuidIdGenerator(),
        ),
        context_builder=ContextBuilder(),
        llm=agent_llm,
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(
            confidence_autonomous=agent_config.guardrails.confidence_autonomous,
            confidence_confirm=agent_config.guardrails.confidence_confirm,
            rate_limit_per_min=agent_config.guardrails.rate_limit_per_min,
        ),
        audit=InMemoryAuditLogger(),
        metrics=metrics,
        capability_provider=ConstantCapabilityProvider(),
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        # P2-commands command-path wiring. Without these three, the
        # Planner's _dispatch_command guard refuses every issue_command
        # with "command dispatch is not configured on this server."
        # StrictCommandGuardrails reads the live capability + resource
        # snapshot at check-time (per-call inside the Planner), so
        # confidence_autonomous is the only value we need to seed.
        # NOTE: ConstantCapabilityProvider returns
        # DeviceResourceStatus(relay_connected=False) by default; the
        # device-status characteristic that flips this flag lands in
        # the P3 BLE bring-up slice. Until then, an issue_command with
        # high confidence will still be refused with
        # "device is not connected to the relay" — a documented
        # accepted behavior for this slice.
        command_validator=StrictCommandValidator(),
        command_guardrails=StrictCommandGuardrails(
            confidence_autonomous=agent_config.guardrails.confidence_autonomous,
        ),
        dispatcher=dispatcher,
        # P1 instruction processor: server-side actions + recent transcript.
        atom_store=atom_store,
        reminder_store=reminder_store,
        recent_transcript=recent_transcript,
    )

    # P3 proactive engine: the same Planner serves both inbound user
    # requests (HTTP /agent) and proactive triggers fired by the
    # extraction worker. The engine never blocks the worker loop —
    # 2s timeout, every drop counted. The placeholder ws_sender is
    # replaced on every WebSocket connect in serve() via
    # ProactiveTriggerEngine.set_ws_sender.
    from openrecall_server.agent.proactive import ProactiveTriggerEngine, plan_timeout_from_env

    class _PlaceholderWsSender:
        """No-op ws_sender. Replaced on every WebSocket connect. If a
        proactive trigger somehow fires before the first connection
        lands, the engine's call raises and is caught+counted — the
        worker is never blocked."""

        async def send_proactive(
            self, *, session_id: str, request_id: str, text: str, atoms,
            propose: dict | None = None,
        ) -> None:
            raise RuntimeError("no active WebSocket connection")

    proactive_engine = ProactiveTriggerEngine(
        planner=planner,
        ws_sender=_PlaceholderWsSender(),
        clock=SystemClock(),
        metrics=metrics,
        ids=UuidIdGenerator(),
        plan_timeout_s=plan_timeout_from_env(__import__("os").environ),
    )

    # Convergence loop for device-level settings (spec §4.2). Shares the
    # agent's dispatcher/ids/clock so a reconciler-issued command is
    # indistinguishable from any other — same signing, same audit trail.
    reconciler = DeviceReconciler(
        settings=settings_store,
        dispatcher=dispatcher,
        ids=UuidIdGenerator(),
        clock=SystemClock(),
        capability_provider=ConstantCapabilityProvider(),
    )

    token = load_or_create_token(args.token_file)
    # Thread the whisper noise-filter thresholds into the streaming
    # transcriber backend.
    from openrecall_server.gateway.adapter import build_pipeline_factory as _bpf
    def _make_factory():
        return _bpf(
            window_ms=args.window_ms,
            hop_ms=1000,
            model=args.model,
            whisper_config=agent_config.whisper,
            asr_config=agent_config.asr,
            speaker_identifier=speaker_identifier,
            audio_store=audio_store,
            # `save_audio` is read once at factory build; `audio_enabled` is
            # read per packet, because it is the backstop for a toggle the
            # user can flip while the device is streaming (spec §4.1).
            persist_audio=settings_store.get().capture.save_audio,
            audio_enabled=lambda: settings_store.get().capture.audio_enabled,
        )
    factory = _make_factory()
    app = build_app(
        token=token,
        get_pubkey=lambda: signer.public_key_bytes,
        event_store=store,
        session_index=session_index,
        session_lifecycle=session_lifecycle,
        # The HTTP API and the WS gateway are on separate ports; advertise the
        # gateway port on /health so the phone's relay can derive its WS URL
        # (it provisions against this HTTP URL and would otherwise reuse the
        # HTTP port for the WS upgrade — "Expected HTTP 101 response").
        gateway_port=args.port,
        planner=planner,
        retriever=planner._retriever,
        atom_store=atom_store,
        metrics=metrics,
        id_generator=UuidIdGenerator(),
        command_store=command_store,
        command_dispatcher=dispatcher,
        # The HTTP control API must share the SAME speaker registry the WS
        # gateway mints speakers into (passed to serve() below). Omitting it
        # leaves app["sense_speaker_registry"]=None, so /speakers/{id}/rename
        # + /speakers/reassign + GET /speakers all return 409/empty even with
        # speaker rec ENABLED — the Android rename then fails with
        # "Couldn't rename on the server". pytest can't catch this (it never
        # imports run_gateway.py), so keep this kwarg wired.
        speaker_registry=speaker_registry,
        segment_index=segment_index,
        segment_meta=segment_meta,
        audio_store=audio_store,
        settings_store=settings_store,
        liveness=liveness,
        reconciler=reconciler,
        capability_provider=ConstantCapabilityProvider(),
        memory_index=memory_index,
        reminders=reminder_store,
        clock=SystemClock(),
    )
    # Tiered retention (spec D8): audio expires, derived text is kept. The
    # asymmetry must be stated plainly in the app's privacy copy.
    retention_sweeper = RetentionSweeper(
        audio_store=audio_store, settings_store=settings_store,
    )
    # P1: reminder sweeper fires due reminders through the process-wide
    # proactive outbox (the same one the proactive engine uses). Tick-driven
    # like the segment + retention sweepers.
    from openrecall_server.reminders.sweeper import ReminderSweeper
    reminder_sweeper = ReminderSweeper(
        store=reminder_store, outbox=proactive_outbox, clock=SystemClock(),
    )

    async def main_loop() -> None:
        http_runner = aiohttp.web.AppRunner(app)
        try:
            await http_runner.setup()
            site = aiohttp.web.TCPSite(http_runner, args.host, args.http_port)
            await site.start()
            # Start the extraction worker so enqueued sessions are
            # processed in the background.
            await worker.start()
            await segment_sweeper.start()
            await retention_sweeper.start()
            await reminder_sweeper.start()
            # P3: register the proactive engine as a worker listener.
            # The engine is fire-and-forget from the worker's POV, so
            # this never blocks extraction.
            worker.add_listener(proactive_engine.on_session_completion)
            # Speaker recognition nudge listener (only when enabled). Its
            # ws_sender is rebound per-connection in serve() alongside the
            # proactive engine's. The listener is best-effort and never
            # re-raises into the worker loop.
            speaker_nudge = None
            if speaker_cfg.enabled:
                from openrecall_server.agent.speaker_nudge import SpeakerNudgeListener
                speaker_nudge = SpeakerNudgeListener(
                    speaker_registry, store, _PlaceholderWsSender(),
                    speaker_cfg,
                    rate_limit_per_min=agent_config.guardrails.rate_limit_per_min,
                    ids=UuidIdGenerator(), clock=SystemClock(),
                )
                worker.add_listener(speaker_nudge.on_session_completion)
                print(
                    f"speaker recognition ENABLED "
                    f"(model={speaker_cfg.embed_model or 'fake'})"
                )
            else:
                print(
                    "speaker recognition DISABLED "
                    "(set OPENRECALL_SPEAKER_ENABLED=true to enable)"
                )
            print(f"http control API on http://{args.host}:{args.http_port}")
            print(f"gateway listening on ws://{args.host}:{args.port}  "
                  f"(window={args.window_ms} ms, events -> {args.db})")
            print(f"bearer token (copy to phone): {token}")
            print(f"server command public key (provision on device): "
                  f"{signer.public_key_bytes.hex()}")
            await serve(
                factory,
                host=args.host,
                port=args.port,
                event_store=store,
                dispatcher=dispatcher,
                token=token,
                session_index=session_index,
                session_lifecycle=session_lifecycle,
                enqueuer=enqueuer,
                proactive_outbox=proactive_outbox,
                proactive_engine=proactive_engine,
                speaker_registry=speaker_registry,
                atom_store=atom_store,
                speaker_nudge=speaker_nudge,
                segment_index=segment_index,
                liveness=liveness,
                reconciler=reconciler,
            )
        finally:
            await retention_sweeper.stop()
            await reminder_sweeper.stop()
            await segment_sweeper.stop()
            await worker.stop()
            await http_runner.cleanup()

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()