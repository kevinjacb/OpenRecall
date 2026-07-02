#!/usr/bin/env python3
"""Extract §G memory atoms from captured §F events using a configurable model.

Runs the out-of-band extraction pass: reads new capture events from the durable
event log and writes structured memories to the atom store. The model is whatever
you configure — local or cloud — via env vars (no model is hardcoded):

    export SENSE_LLM_MODEL=gemma2                       # required: the model name
    export SENSE_LLM_BASE_URL=http://localhost:11434/v1  # default: Ollama
    export SENSE_LLM_API_KEY=...                          # optional (cloud)
    pip install -e '.[llm]'
    python scripts/extract_memories.py --session <session_id>

Examples of "any model, local or cloud":
  * local Gemma via Ollama:   BASE_URL=http://localhost:11434/v1  MODEL=gemma2
  * local Qwen via mlx_lm:     BASE_URL=http://localhost:8080/v1   MODEL=qwen2.5
  * cloud (e.g. MiniMax/OpenAI): BASE_URL=<provider>/v1  MODEL=<name>  API_KEY=<key>
"""

from __future__ import annotations

import argparse
import os

from sense_server.events.store import SqliteEventStore
from sense_server.memory.extract import LLMExtractor
from sense_server.memory.llm import OpenAICompatibleChatModel
from sense_server.memory.pipeline import ExtractionPipeline
from sense_server.memory.store import SqliteAtomStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="session_id to extract")
    ap.add_argument("--events-db", default="data/events.db")
    ap.add_argument("--atoms-db", default="data/atoms.db")
    args = ap.parse_args()

    model = OpenAICompatibleChatModel.from_env(os.environ)
    print(f"extracting with model={model.model} via {model.base_url}")

    pipe = ExtractionPipeline(
        event_store=SqliteEventStore(args.events_db),
        atom_store=SqliteAtomStore(args.atoms_db),
        extractor=LLMExtractor(model),
    )
    produced = pipe.run(args.session)

    print(f"produced {len(produced)} new memory atom(s) for session {args.session!r}:")
    for atom in produced:
        print(f"  [{atom.kind}] {atom.text}  (from {atom.source_event_id})")


if __name__ == "__main__":
    main()
