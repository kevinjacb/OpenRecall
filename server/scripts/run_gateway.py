#!/usr/bin/env python3
"""Run the OpenRecall gateway WebSocket server + HTTP control API.

This is the live entry point the Android relay connects to (XIAO -> Android -> Mac).
It wires the real Opus decoder + MLX-whisper transcriber per session, so it needs
the heavy extras installed on the Mac:

    pip install -e '.[mlx,opus]'   # also: brew install opus
    python scripts/run_gateway.py --port 8765
    # Parakeet backend (low-latency live transcription): OPENRECALL_ASR_BACKEND=parakeet
    #   pip install -e '.[parakeet]'  (parakeet-mlx>=0.5)
    # OPENRECALL_ASR_MODE=auto|utterance|hop — transcription scheduling.
    #   auto (default): utterance for parakeet (one ASR call per utterance,
    #   emitted ~1s after the wearer pauses; no overlapping re-transcription),
    #   hop for whisper (the rolling-window streaming path).

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

Environment variables (command channel, all optional — off by default):

  OPENRECALL_COMMAND_DETECTOR_ENABLED   set "true" to enable the speech→command
        channel (default: off). When off, spoken commands never fire.
  OPENRECALL_COMMAND_REQUIRE_WEARER     "true" (default) restricts voice commands to
        the confirmed wearer; "false" allows any identified speaker.
  OPENRECALL_COMMAND_PHRASES            comma list of phrase→type overrides, e.g.
        "take a photo=capture_photo,cheese=capture_photo". Unset = DEFAULT_COMMAND_PHRASES.
  OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD  Stage 2 LLM confidence floor (default 0.8).
  OPENRECALL_COMMAND_COOLDOWN_S         per-command-type cooldown seconds (default 3).
  OPENRECALL_COMMAND_MAX_INFLIGHT       concurrent Stage 2 calls per session (default 1).
  OPENRECALL_COMMAND_LLM_MODEL          dedicated Stage 2 model; unset reuses the
        extractor's shared chat model.
  OPENRECALL_COMMAND_LLM_BASE_URL       Stage 2 base URL; unset reuses shared.
  OPENRECALL_COMMAND_LLM_API_KEY        Stage 2 API key; unset reuses shared.

The channel is two-stage (keyword pre-filter → scoped LLM confirmation) and dispatches
through the rule-based command path (no Planner, no retrieval). It is gated on the
confirmed wearer by default; requires OPENRECALL_SPEAKER_ENABLED for speaker assignment.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

import aiohttp.web

from openrecall_server.agent.config import scheduling_family
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
from openrecall_server.memory.backfill import backfill_index_occurred_at, backfill_occurred_at
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
from openrecall_server.storage_paths import (
    InvalidDatabasePath,
    sibling_db,
    validate_events_db_path,
)


def _probe_inference(cfg) -> dict | None:
    """GET /info from the inference service, or None if it cannot be reached.

    Best-effort and short-deadline: this runs on the startup path, and a slow
    or dead service must delay the boot by ~2s, not block it. Everything the
    caller does with the result is advisory.
    """
    import json
    import urllib.request

    try:
        url = cfg.url.rstrip("/") + "/info"
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--window-ms", type=int, default=None,
        help="transcription rolling-window size in ms. None = backend-aware: "
             "5000 for whisper (context-hungry: accuracy depends on surrounding "
             "speech), 2000 for parakeet (transducer, tuned for low-latency RTF). "
             "The low-latency refactor cut this 5000->2000 for parakeet's RTF but "
             "applied it to whisper too, gutting whisper's context window.")
    ap.add_argument("--hop-ms", type=int, default=None,
                    help="transcription hop (output granularity); MUST be a multiple of "
                         "FRAME_MS=20 (pipeline enforces it). None = backend-aware: "
                         "240 for parakeet (the smallest multiple satisfying the Phase-0 "
                         "sustainability inequality: 140ms inference < 240*0.6=144ms; "
                         "spec §6), 1000 for whisper (a large-v3-turbo call on a 5s "
                         "window cannot sustain 4 calls/s — a 240ms hop overloads the "
                         "ASR worker and forces packet drops).")
    ap.add_argument("--model", default=None, help="override the MLX-whisper model repo")
    ap.add_argument("--db", default="data/events.db", help="durable capture-event store path")
    ap.add_argument("--key-file", default="data/server_ed25519.key",
                    help="server command-signing key (created on first run)")
    ap.add_argument("--token-file", default="data/server_token",
                    help="bearer token file for the HTTP control API + WS auth (created on first run)")
    ap.add_argument("--http-port", type=int, default=8766,
                    help="HTTP control API port (operator/phone-facing)")
    ap.add_argument(
        "--advertised-gateway-port", type=int, default=None,
        help="the WS port /health advertises, when it differs from the port "
             "the gateway binds. The phone builds its WebSocket URL as "
             "wss://<http-host>:<this>, so behind a reverse proxy or a "
             "Cloudflare Tunnel — where the public edge is 443 and 8765 is not "
             "served — the bind port is the wrong answer. Pass 443 there, or 0 "
             "to omit the field entirely, which makes the phone fall back to "
             "the port already in its HTTP URL. Default: the bind port.")
    ap.add_argument(
        "--config", default="config.toml",
        help="TOML config file for OPENRECALL_* runtime knobs (asr backend, "
             "denoise, whisper params, command detector, llm/vlm, etc.) — a "
             "persistent alternative to exporting env vars each launch. Keys "
             "flatten to OPENRECALL_{SECTION}_{KEY}; real env vars still win "
             "(setdefault). A missing file is a no-op. Copy config.example.toml "
             "to config.toml to get started.",
    )
    args = ap.parse_args()

    # Apply the config file BEFORE any OPENRECALL_* read (LOG_LEVEL below, the
    # sentence-coalesce / denoise reads later, and load_agent_config all read
    # os.environ). setdefault means an explicit env var or CLI override still
    # wins; the file only fills gaps. A missing file is a silent no-op.
    from openrecall_server.config_file import apply_config_file
    _cfg_applied = apply_config_file(args.config)

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
    if _cfg_applied:
        logging.getLogger(__name__).info(
            "config: %d key(s) from %s (env var wins on conflict)",
            len(_cfg_applied), args.config,
        )

    # Validates that the sibling databases can actually be derived from this
    # path. A basename without "events.db" used to make every derivation a
    # no-op, silently opening all eight stores on one file (storage_paths).
    try:
        validate_events_db_path(args.db)
    except InvalidDatabasePath as exc:
        raise SystemExit(str(exc)) from None   # a flag error, not a crash

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(args.db)
    signer = load_or_create_signer(args.key_file)
    from openrecall_server.commands.store import SqliteCommandStore
    command_store = SqliteCommandStore(
        sibling_db(args.db, "commands.db")
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
        sibling_db(args.db, "segment_meta.db")
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
        sibling_db(args.db, "settings.db")
    )

    # Memory pipeline (M4.4 wiring): the gateway offloads extraction +
    # embedding + indexing to an out-of-band worker so the audio hot path
    # stays real-time. The enqueuer is the only seam the gateway hot path
    # touches; the worker drains it on a background task. The metrics
    # recorder is process-wide so /metrics reflects the same numbers.
    metrics = InMemoryMetricsRecorder()
    atom_store = SqliteAtomStore(sibling_db(args.db, "atoms.db"))
    # P3 vision: content-addressed image blobs + the rel_ts->session sidecar.
    from openrecall_server.media.blob import FilesystemBlobStore
    blob_store = FilesystemBlobStore(Path(args.db).parent / "blobs")
    from openrecall_server.sessions.timeline import SessionTimelineIndex
    session_timeline = SessionTimelineIndex()
    # Vision pipeline is None when no VLM is configured (the upload route 503s);
    # DummyVisionModel for tests/CI; else the OpenAI-compatible VLM from env.
    vision_pipeline = None
    vlm_model = __import__("os").environ.get("OPENRECALL_VLM_MODEL")
    if vlm_model:
        from openrecall_server.vision.model import (
            DummyVisionModel, OpenAICompatibleVisionModel,
        )
        from openrecall_server.vision.pipeline import VisionPipeline
        if vlm_model == "dummy":
            vision_model = DummyVisionModel()
        else:
            vision_model = OpenAICompatibleVisionModel.from_env(__import__("os").environ)
        vision_pipeline = VisionPipeline(
            blob_store, vision_model, atom_store, clock=SystemClock())
        logging.info("vision pipeline enabled (model=%s)", vlm_model)
    else:
        logging.warning(
            "OPENRECALL_VLM_MODEL unset — vision disabled "
            "(POST /media/snapshots will 503)")
    # P1 instruction processor: reminders side table + recent-transcript
    # provider. The reminder store is a separate DB file (twin to atoms.db);
    # the recent-transcript provider reads the event store the gateway
    # already owns.
    from openrecall_server.reminders.store import SqliteReminderStore
    reminder_store = SqliteReminderStore(sibling_db(args.db, "reminders.db"))
    from openrecall_server.agent.context import EventBackedRecentTranscript
    recent_transcript = EventBackedRecentTranscript(store)
    # Spec §1.1: give historical atoms their real conversation time, joined
    # from the source capture event. Idempotent and best-effort — a row that
    # can't be resolved keeps the created_at fallback.
    backfill_occurred_at(store, atom_store)
    memory_index = SqliteMemoryIndex(sibling_db(args.db, "memory_index.db"))
    # Task 1 (retrieval-clock production fix): copy occurred_at from the atom
    # store onto the vector index so the production retrieval path (which
    # scores recency on timeline_at) has real values instead of always
    # falling back to created_at. Must run after the atom-store backfill
    # above — it depends on atoms already having occurred_at filled.
    # Idempotent and best-effort; a failure here must never block boot.
    backfill_index_occurred_at(atom_store, memory_index)
    # Speaker recognition: a Sqlite registry + an identifier wired into the
    # pipeline only when OPENRECALL_SPEAKER_ENABLED=true. Off by default — when
    # disabled, build_speaker_identifier returns None and the pipeline wires
    # no identifier, so zero embed calls run and every Transcript carries
    # speaker=None.
    from openrecall_server.agent.config import load_agent_config
    from openrecall_server.gateway.adapter import build_speaker_identifier
    from openrecall_server.ingest.speaker_config import load_speaker_config
    from openrecall_server.memory.speaker_registry import SqliteSpeakerRegistry

    # Read OPENRECALL_CONFIDENCE_AUTONOMOUS / OPENRECALL_CONFIDENCE_CONFIRM /
    # OPENRECALL_RATE_LIMIT_PER_MIN (and the rest of the policy) from the
    # process environment. Defaults match the spec; a bad value aborts startup
    # with a clear error. Loaded here rather than further down because the
    # speaker embedder below needs `.inference` to know where to run.
    agent_config = load_agent_config(__import__("os").environ)
    speaker_cfg = load_speaker_config(__import__("os").environ)
    speaker_registry = SqliteSpeakerRegistry(
        sibling_db(args.db, "speakers.db"), speaker_cfg,
    )
    speaker_identifier = build_speaker_identifier(
        speaker_cfg, speaker_registry, embedder=None,
        # P1: unset (the default) keeps the embedder in-process; a url points
        # it at the inference service.
        inference_config=agent_config.inference,
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
    # guardrails, durable audit log, and a capability provider backed by
    # device telemetry. The retriever and atoms store are the same ones
    # the worker populates, so a freshly-extracted atom is immediately
    # queryable.
    # P2 power/sleep: ONE shared ReportedCapabilityProvider feeds the
    # Planner's command guardrails (reads reported battery per-call),
    # the DeviceReconciler, build_app (/device/status), and GatewayCore
    # (on_telemetry updates it). So the same reported battery/state is
    # consistent across status, admission, and reconciliation. Defaults
    # (battery=1.0, state="unknown") match ConstantCapabilityProvider,
    # so guardrails clear all floors until the first telemetry lands.
    from openrecall_server.agent.audit import InMemoryAuditLogger
    from openrecall_server.agent.capability import ReportedCapabilityProvider
    capability_provider = ReportedCapabilityProvider(
        vision_enabled=lambda: settings_store.get().capture.vision_enabled,
    )
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
    # `agent_config` was loaded above, alongside the speaker config.
    # Resolve the backend-aware transcription window and hop. Whisper is
    # context-hungry (5s window, as the streaming design was built around);
    # Parakeet's transducer runs at 2s for low-latency RTF. Whisper also gets
    # a 1000ms hop: a large-v3-turbo call on a 5s window cannot sustain the
    # 240ms cadence Parakeet was tuned for — the ASR worker queue overflows
    # and drops real audio packets (chunk_seq holes), which shreds the stream.
    # Explicit --window-ms / --hop-ms always win.
    # The split is per *engine family*, not per backend name: faster_whisper is
    # Whisper (on CTranslate2), so it wants Whisper's 5s window and 1s hop, not
    # Parakeet's low-latency 2s/240ms — a full Whisper decode cannot sustain a
    # 240ms cadence on any device.
    if args.window_ms is None:
        args.window_ms = 2000 if agent_config.asr.backend == "parakeet" else 5000
        logging.info(
            "window_ms auto=%d (asr_backend=%s)", args.window_ms, agent_config.asr.backend,
        )
    if args.hop_ms is None:
        args.hop_ms = 240 if agent_config.asr.backend == "parakeet" else 1000
        logging.info(
            "hop_ms auto=%d (asr_backend=%s)", args.hop_ms, agent_config.asr.backend,
        )
    retriever = Retriever(
        embedder=embedder,
        index=memory_index,
        scorer=SimRecencyScorer(),
        clock=SystemClock(),
        ids=UuidIdGenerator(),
    )
    base_planner = Planner(
        retriever=retriever,
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
        capability_provider=capability_provider,
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        # P2-commands command-path wiring. Without these three, the
        # Planner's _dispatch_command guard refuses every issue_command
        # with "command dispatch is not configured on this server."
        # StrictCommandGuardrails reads the live capability + resource
        # snapshot at check-time (per-call inside the Planner), so
        # confidence_autonomous is the only value we need to seed.
        # NOTE: ReportedCapabilityProvider.resources() returns
        # DeviceResourceStatus with relay_connected matching the
        # DeviceResourceStatus default (True). The device-status
        # characteristic that flips this flag lands in a later BLE
        # bring-up slice; until then the relay-connected check is inert
        # — a documented accepted behavior for this slice.
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

    # P3 proactive engine. The engine never blocks the worker loop —
    # 2s timeout, every drop counted. The placeholder ws_sender is
    # replaced on every WebSocket connect in serve() via
    # ProactiveTriggerEngine.set_ws_sender.
    #
    # Deliberately wired to `base_planner`, NOT the backend-selected
    # `planner` constructed below (§5.2 [agent] backend) — and this is
    # intentional, not an ordering accident. `[agent] backend` selects the
    # HTTP /agent path only. Proactive intentionally does not follow it
    # until the §5.5 queue/triage redesign ships (a real transport is not
    # safe to drive from an unattended background trigger yet). Do not
    # "fix" this by hoisting the backend-selection block above this point
    # and passing `planner` here — that would route proactive traffic to
    # an out-of-process agent the moment Phase 3 lands a transport, which
    # is exactly what §5.5 forbids.
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
        planner=base_planner,
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
        capability_provider=capability_provider,
    )

    token = load_or_create_token(args.token_file)
    # Thread the whisper noise-filter thresholds into the streaming
    # transcriber backend.
    import os as _os
    # Sentence coalescing: group per-hop word Segments into readable
    # sentences. ON by default (the low-latency hop emits ~1 word/hop, which
    # would otherwise be one word per Transcript line / one event per word).
    # Set OPENRECALL_SENTENCE_COALESCE=0 to revert to per-word emission
    # (for real-hardware debugging), and OPENRECALL_SENTENCE_PAUSE_MS to tune
    # the inter-sentence pause threshold (default 1000 ms; 400 ms fired inside
    # a natural thinking pause and split mid-thought utterances).
    _sentence_coalesce = _os.environ.get("OPENRECALL_SENTENCE_COALESCE", "1") != "0"
    try:
        _sentence_pause_ms = int(_os.environ.get("OPENRECALL_SENTENCE_PAUSE_MS", "1000"))
    except ValueError:
        raise SystemExit(
            "OPENRECALL_SENTENCE_PAUSE_MS must be an integer number of milliseconds"
        )
    if _sentence_pause_ms <= 0:
        raise SystemExit("OPENRECALL_SENTENCE_PAUSE_MS must be > 0")
    # Server-side denoise: spectral gating on the decoded PCM before it reaches
    # the transcriber and the speaker embedder. Off by default (NoopDenoiser,
    # zero cost / zero behavior change); set OPENRECALL_DENOISE_ENABLED=1 to
    # wire NoisereduceDenoiser, which self-calibrates a noise profile from the
    # quietest hops and gates each hop against it. Requires the [denoise] extra
    # (pip install -e '.[denoise]'); the noisereduce import is lazy so a default
    # install and the unit suite run without it.
    _denoise_enabled = _os.environ.get("OPENRECALL_DENOISE_ENABLED", "0") not in (
        "0", "", "false",
    )
    _denoiser = None
    if _denoise_enabled:
        from openrecall_server.ingest.denoise import build_denoiser
        _denoiser = build_denoiser(enabled=True, sample_rate=16000)
        logging.info("denoise: enabled (noisereduce spectral gating)")
    from openrecall_server.gateway.adapter import build_pipeline_factory as _bpf
    def _make_factory():
        return _bpf(
            window_ms=args.window_ms,
            hop_ms=args.hop_ms,
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
            rel_ts_sink=session_timeline.record,
            sentence_coalesce=_sentence_coalesce,
            sentence_pause_ms=_sentence_pause_ms,
            denoiser=_denoiser,
            # P1: unset (the default) keeps ASR in-process; a url points it at
            # the inference service, so this box need not own the model.
            inference_config=agent_config.inference,
        )
    factory = _make_factory()
    # Say which side of the P1 boundary this process is on. Without this the
    # only way to tell an operator's remote gateway from a silently-in-process
    # one is to watch for GPU load on the other box.
    if agent_config.inference.url:
        # Probe /info rather than asserting reachability. The banner used to
        # claim REMOTE whether or not anything was listening, and the speaker
        # warmup that follows is now a network call wrapped in a best-effort
        # except — so a gateway pointed at a dead service came up looking
        # healthy and failed on every frame.
        remote = _probe_inference(agent_config.inference)
        detail = (f"remote asr={remote.get('asr_backend', '?')}, "
                  f"ready={remote.get('ready', '?')}" if remote
                  else "UNREACHABLE — transcription will fail on every frame")
        print(f"inference: REMOTE at {agent_config.inference.url} "
              f"(timeout {agent_config.inference.timeout_s}s, {detail})")

        # The two processes pick their ASR engine from their own environments,
        # and nothing reconciles them. The gateway still derives *scheduling*
        # locally — window/hop above, and hop-vs-utterance mode — while the
        # engine is whatever the service chose. A mismatch is silent and
        # produces bad transcripts rather than an error:
        #   gateway=parakeet, service=whisper -> a 240ms hop against
        #     large-v3-turbo, which this file's own --hop-ms help calls out as
        #     overloading the ASR worker into dropping real audio packets.
        #   gateway=whisper, service=parakeet -> hop mode re-transcribing
        #     overlapping 5s windows with Parakeet, whose timestamps shift
        #     between overlapping calls — the word-doubling that utterance
        #     mode exists to prevent.
        remote_asr = (remote or {}).get("asr_backend", "")
        # Compare SCHEDULING FAMILY, not name. What breaks is a cadence
        # mismatch, and the cadence is chosen by family: parakeet gets
        # 2000ms/240ms and utterance mode, every Whisper variant gets
        # 5000ms/1000ms and hop mode. So "whisper" driving a
        # FasterWhisperStreamingBackend is CORRECT — same decoder, same
        # cadence — and flagging it would train the operator to ignore this
        # line. Only a cross-family pairing is a real problem.
        _family = scheduling_family(agent_config.asr.backend)
        _remote_family = scheduling_family(remote_asr)
        if remote_asr and _remote_family != _family:
            logging.error(
                "ASR BACKEND MISMATCH: this gateway schedules for the %r family "
                "(window=%sms hop=%sms) but the inference service runs %s (%r "
                "family). Set OPENRECALL_ASR_BACKEND compatibly in both "
                "processes; otherwise expect dropped audio packets or "
                "duplicated words.",
                _family, args.window_ms, args.hop_ms, remote_asr, _remote_family,
            )
    else:
        print("inference: in-process "
              "(set OPENRECALL_INFERENCE_URL to use the inference service)")

    # --- MCP (spec §5.2) -------------------------------------------------
    # A *second* bearer token, distinct from the relay's: the hermes
    # principal reaches /mcp and nothing else, the relay reaches everything
    # else and never /mcp (mcp/principal.py). One shared token would let a
    # compromised MCP client drive the whole control API.
    #
    # Without these three kwargs the hermes principal is unreachable outside
    # tests and POST /mcp answers 403 — the middleware treats the caller as
    # the relay principal and `may_reach` blocks it before the request ever
    # reaches the route (mcp/principal.py) — and pytest can't catch that,
    # because it never imports this file (same trap as speaker_registry
    # above). Smoke it by hand after editing.
    from openrecall_server.mcp.ledger import RequestLedger
    from openrecall_server.mcp.tools import build_registry as build_mcp_registry

    hermes_token = load_or_create_token(
        str(Path(args.db).with_name("hermes.token")))
    mcp_ledger = RequestLedger(SystemClock())
    mcp_registry = build_mcp_registry(
        retriever=retriever,
        atom_store=atom_store,
        session_index=session_index,
        speaker_registry=speaker_registry,
        capability_provider=capability_provider,
        ledger=mcp_ledger,
    )
    # `print`, not `logging.info`, matching the relay token below: a secret
    # handed to the logging system reaches every configured handler, including
    # files and any aggregator an operator has attached.
    print(f"hermes bearer token (for MCP clients): {hermes_token} "
          "(all six tools callable once a request is open; but this process "
          "opens none — SystemExit fires below for every [agent] backend "
          "other than \"planner\", so nothing here calls agent.respond)")

    # Which reasoning layer serves POST /agent — the HTTP path only. The
    # proactive path (ProactiveTriggerEngine, constructed above) does NOT
    # follow this switch; see the comment at its construction site.
    # "planner" (the default) is the pre-Hermes behaviour and the rollback.
    _backend = agent_config.backend.backend
    if _backend == "planner":
        planner = base_planner
    else:
        # Phase 2 ships no real transport. Selecting a hermes backend without
        # one is a configuration error, not a silent downgrade.
        raise SystemExit(
            f"{_backend!r} is not available yet: Phase 2 ships no Hermes "
            f"transport. Set [agent] backend = \"planner\" (or unset "
            f"OPENRECALL_AGENT_BACKEND) until Phase 3 installs one."
        )

    app = build_app(
        token=token,
        hermes_token=hermes_token,
        mcp_registry=mcp_registry,
        mcp_ledger=mcp_ledger,
        get_pubkey=lambda: signer.public_key_bytes,
        event_store=store,
        session_index=session_index,
        session_lifecycle=session_lifecycle,
        # The HTTP API and the WS gateway are on separate ports; advertise the
        # gateway port on /health so the phone's relay can derive its WS URL
        # (it provisions against this HTTP URL and would otherwise reuse the
        # HTTP port for the WS upgrade — "Expected HTTP 101 response").
        # None -> the bind port. 0 -> omit from /health, so the phone reuses
        # the port from its HTTP URL (443 behind a tunnel).
        gateway_port=(
            args.port if args.advertised_gateway_port is None
            else (args.advertised_gateway_port or None)
        ),
        planner=planner,
        retriever=retriever,
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
        blob_store=blob_store,
        vision=vision_pipeline,
        session_timeline=session_timeline,
        liveness=liveness,
        reconciler=reconciler,
        capability_provider=capability_provider,
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
    # P3 vision retention: sweep old image blobs hourly (scene atoms kept).
    from openrecall_server.vision.retention import sweep_vision_retention

    async def _retention_loop():
        while True:
            try:
                deleted = sweep_vision_retention(
                    atom_store, blob_store,
                    snapshot_days=settings_store.get().retention.snapshot_days,
                    now=SystemClock().now(),
                )
                if deleted:
                    logging.info("vision retention swept %d image blobs", deleted)
            except Exception:
                logging.exception("vision_retention_sweep_failed")
            await asyncio.sleep(3600)

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
            asyncio.create_task(_retention_loop())
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
                    f"(model={speaker_cfg.embed_model or 'resemblyzer'})"
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
            # Speech→command channel (off by default). Constructed inside main_loop
            # so asyncio.get_running_loop() binds the loop this server runs on —
            # the detector's call_soon_threadsafe must target THIS loop or Stage 2
            # never fires.
            command_detector = None
            command_memory_writer = None
            if agent_config.command.enabled:
                from openrecall_server.agent.command_detector import CommandDetector
                from openrecall_server.agent.command_memory import CommandMemoryWriter
                # StrictCommandValidator + UuidIdGenerator are already in scope
                # (imported at the top of main()); re-importing them here would
                # shadow the closure binding and break the speaker_nudge block
                # above, which references UuidIdGenerator before this branch runs
                # (UnboundLocalError at startup with SPEAKER_ENABLED=true).

                # Stage 2 LLM: dedicated model if OPENRECALL_COMMAND_LLM_MODEL is set,
                # else reuse the extractor's shared chat model (llm_chat).
                if agent_config.command.llm_model:
                    command_llm = OpenAICompatibleChatModel(
                        base_url=agent_config.command.llm_base_url or llm_chat.base_url,
                        model=agent_config.command.llm_model,
                        api_key=agent_config.command.llm_api_key,
                    )
                else:
                    command_llm = llm_chat

                command_detector = CommandDetector(
                    model=command_llm,
                    dispatcher=dispatcher,
                    command_validator=StrictCommandValidator(),
                    capability_provider=capability_provider,  # the same one the Planner uses
                    speaker_registry=speaker_registry,
                    loop=asyncio.get_running_loop(),
                    config=agent_config.command,
                    ids=UuidIdGenerator(),
                    clock=SystemClock(),
                )
                command_memory_writer = CommandMemoryWriter(
                    store=atom_store, clock=SystemClock(),
                )
                logging.info("command detector enabled (require_wearer=%s)",
                             agent_config.command.require_wearer)
            else:
                logging.info("command detector disabled "
                             "(set OPENRECALL_COMMAND_DETECTOR_ENABLED=true to enable)")
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
                # P2 power/sleep: forward the shared capability provider
                # (on_telemetry reports battery/state into it) and the
                # settings store (on_telemetry clears desired sleep_mode
                # on a button wake) into every per-connection GatewayCore.
                capability_provider=capability_provider,
                settings=settings_store,
                command_detector=command_detector,
                command_memory_writer=command_memory_writer,
            )
        finally:
            await retention_sweeper.stop()
            await reminder_sweeper.stop()
            await segment_sweeper.stop()
            await worker.stop()
            await http_runner.cleanup()

    # `docker stop` and systemd send SIGTERM, whose default action kills the
    # process outright — so the `finally` above never runs, the extraction
    # worker is not drained, the sweepers are not stopped, and the HTTP runner
    # is not cleaned up. Map it onto the SIGINT path, which already does all of
    # that correctly and is the one exercised by every Ctrl-C in development.
    def _terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()