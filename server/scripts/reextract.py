#!/usr/bin/env python3
"""Re-extract memory atoms from captured events — the recovery path.

Resets every session's extraction cursor and re-runs the windowed extraction
pipeline against the full event log, so memories that were never formed
(because a cursor was advanced by an older, broken extractor) are recovered.

When to use this
---------------
* After bumping ``EXTRACTOR_VERSION`` in ``run_gateway.py`` you do NOT need
  this script — the worker's reconcile-on-start re-extracts any session whose
  cursor was stamped with the old version automatically.
* Use this script when you want to force a full re-extract *right now*
  regardless of cursor state — e.g. after a prompt change you forgot to bump
  the version for, after restoring ``events.db`` from backup, or to verify
  extraction end-to-end against live data.

It uses the SAME windowed pipeline the gateway runs (``memory.stages.Pipeline``),
not the legacy per-event extractor, so it forms the same memories a live
session would. The atom store and index are idempotent on ``atom_id``, so
re-running is safe — already-stored atoms are not duplicated.

The LLM + embedder are configured via the same ``SENSE_LLM_*`` / ``SENSE_EMBED_*``
env vars as ``run_gateway.py`` (no model is hardcoded). Export them first —
e.g. ``set -a; source .env; set +a`` — the same way you launch the gateway.

    pip install -e '.[llm]'
    python scripts/reextract.py --events-db data/events.db
    python scripts/reextract.py --session <session_id>   # one session only
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.events.store import SqliteEventStore
from sense_server.memory.embeddings import OpenAICompatibleEmbedder
from sense_server.memory.extraction_worker import ExtractionWorker
from sense_server.memory.extract import LLMExtractor, LLMParseError
from sense_server.memory.index import SqliteMemoryIndex
from sense_server.memory.llm import OpenAICompatibleChatModel
from sense_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from sense_server.memory.store import SqliteAtomStore

# Must match EXTRACTOR_VERSION in run_gateway.py so cursors this script stamps
# are honoured by the live worker (and vice versa).
EXTRACTOR_VERSION = "v1"


def _utcnow() -> "datetime":
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events-db", default="data/events.db")
    ap.add_argument("--atoms-db", default="data/atoms.db")
    ap.add_argument("--index-db", default="data/memory_index.db")
    ap.add_argument("--session", default=None,
                    help="only re-extract this session_id (default: all sessions)")
    ap.add_argument("--keep-cursors", action="store_true",
                    help="do not reset cursors; only re-extract sessions whose "
                         "recorded cursor version mismatches the current extractor")
    args = ap.parse_args()

    env = os.environ
    embedder = OpenAICompatibleEmbedder.from_env(env)
    chat = OpenAICompatibleChatModel.from_env(env)
    print(f"re-extracting with LLM={chat.model} via {chat.base_url}, "
          f"embedder={embedder.model} via {embedder.base_url}")
    print(f"extractor_version={EXTRACTOR_VERSION}")

    events = SqliteEventStore(args.events_db)
    atoms = SqliteAtomStore(args.atoms_db)
    index = SqliteMemoryIndex(args.index_db)
    metrics = InMemoryMetricsRecorder()

    pipeline = Pipeline(
        extraction=ExtractionStage(
            extractor=LLMExtractor(chat),
            clock=_utcnow,
            version=EXTRACTOR_VERSION,
        ),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=embedder),
        indexing=IndexingStage(index=index),
        store=atoms,
    )
    worker = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=metrics, extractor_version=EXTRACTOR_VERSION,
    )

    sessions = [args.session] if args.session else list(events.sessions())
    if not sessions:
        print("no sessions in event store; nothing to do")
        return

    if not args.keep_cursors:
        # Force a full re-extract: drop every cursor row so get_cursor returns
        # -1 for every session. The worker re-stamps them with the current
        # version as it re-extracts.
        conn = sqlite3.connect(args.atoms_db)
        n = conn.execute("DELETE FROM extraction_cursor").rowcount
        conn.commit()
        conn.close()
        print(f"reset {n} cursor row(s) for full re-extract")

    print(f"re-extracting {len(sessions)} session(s)...")
    t0 = time.monotonic()
    total_new = 0
    failed: list[str] = []
    for sid in sorted(sessions):
        before = len(atoms.atoms(sid))
        try:
            new = worker.process_session(sid)
        except LLMParseError as e:
            failed.append(f"{sid[:8]}: parse failure: {e}")
            print(f"  {sid[:8]}: PARSE FAIL ({e})")
            continue
        except Exception as e:  # noqa: BLE001
            failed.append(f"{sid[:8]}: {type(e).__name__}: {e}")
            print(f"  {sid[:8]}: FAIL {type(e).__name__}: {e}")
            continue
        after = len(atoms.atoms(sid))
        cursor = atoms.get_cursor(sid, extractor_version=EXTRACTOR_VERSION)
        print(f"  {sid[:8]}: +{len(new)} new atom(s) "
              f"({before} -> {after} stored, cursor={cursor})")
        total_new += len(new)

    dt = time.monotonic() - t0
    print(f"\ndone: +{total_new} new atom(s) across {len(sessions)} session(s) "
          f"in {dt:.0f}s")
    if failed:
        print(f"{len(failed)} session(s) failed:")
        for f in failed:
            print(f"  {f}")
    # Surface the live counters so a silent run is auditable.
    rendered = metrics.render().strip()
    if rendered:
        print("metrics:\n" + rendered)


if __name__ == "__main__":
    main()