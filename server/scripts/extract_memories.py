#!/usr/bin/env python3
"""Extract §G memory atoms from captured §F events for one session.

Runs the SAME windowed extraction pipeline the gateway runs
(``memory.stages.Pipeline``) — not the legacy per-event extractor — so it forms
the same memories a live session would. Reads capture events past the
session's extraction cursor, extracts/ embeds/ indexes them, and advances the
cursor (H7: only after extraction + indexing both succeed).

The model is whatever you configure — local or cloud — via env vars (no model
is hardcoded), the same ``OPENSAPIEN_LLM_*`` / ``OPENSAPIEN_EMBED_*`` vars as
``run_gateway.py``. Export them first, e.g. ``set -a; source .env; set +a``.

    pip install -e '.[llm]'
    python scripts/extract_memories.py --session <session_id>

To re-extract EVERY session (e.g. recovering from a stale-cursor lockout),
use ``scripts/reextract.py`` instead.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from opensapien_server.agent.metrics import InMemoryMetricsRecorder
from opensapien_server.events.store import SqliteEventStore
from opensapien_server.memory.embeddings import OpenAICompatibleEmbedder
from opensapien_server.memory.extraction_worker import ExtractionWorker
from opensapien_server.memory.extract import LLMExtractor
from opensapien_server.memory.index import SqliteMemoryIndex
from opensapien_server.memory.llm import OpenAICompatibleChatModel
from opensapien_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from opensapien_server.memory.store import SqliteAtomStore

# Must match EXTRACTOR_VERSION in run_gateway.py.
EXTRACTOR_VERSION = "v1"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="session_id to extract")
    ap.add_argument("--events-db", default="data/events.db")
    ap.add_argument("--atoms-db", default="data/atoms.db")
    ap.add_argument("--index-db", default="data/memory_index.db")
    args = ap.parse_args()

    env = os.environ
    chat = OpenAICompatibleChatModel.from_env(env)
    embedder = OpenAICompatibleEmbedder.from_env(env)
    print(f"extracting with LLM={chat.model} via {chat.base_url}, "
          f"embedder={embedder.model} via {embedder.base_url}")

    events = SqliteEventStore(args.events_db)
    atoms = SqliteAtomStore(args.atoms_db)
    index = SqliteMemoryIndex(args.index_db)
    pipeline = Pipeline(
        extraction=ExtractionStage(
            extractor=LLMExtractor(chat),
            clock=lambda: datetime.now(timezone.utc),
            version=EXTRACTOR_VERSION,
        ),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=embedder),
        indexing=IndexingStage(index=index),
        store=atoms,
    )
    worker = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=InMemoryMetricsRecorder(),
        extractor_version=EXTRACTOR_VERSION,
    )

    produced = worker.process_session(args.session)

    print(f"produced {len(produced)} new memory atom(s) for session {args.session!r}:")
    for atom in produced:
        print(f"  [{atom.kind}] {atom.text}  (from {atom.source_event_id})")


if __name__ == "__main__":
    main()