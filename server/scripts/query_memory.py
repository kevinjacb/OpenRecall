#!/usr/bin/env python3
"""Ask your memory a question: semantic search over extracted §G atoms.

Indexes any not-yet-indexed atoms (idempotent), then returns the best matches for a
query. The embedding model is configurable — local or cloud — via env (nothing
hardcoded):

    export SENSE_EMBED_MODEL=nomic-embed-text                # required
    export SENSE_EMBED_BASE_URL=http://localhost:11434/v1     # default: Ollama
    export SENSE_EMBED_API_KEY=...                            # optional (cloud)
    pip install -e '.[llm]'
    python scripts/query_memory.py --session <id> --query "what did he say about tea?"
"""

from __future__ import annotations

import argparse
import os

from sense_server.memory.embeddings import OpenAICompatibleEmbedder
from sense_server.memory.index import SqliteMemoryIndex
from sense_server.memory.retrieval import IndexingPipeline, MemoryRetriever
from sense_server.memory.store import SqliteAtomStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("-k", type=int, default=5, help="number of results")
    ap.add_argument("--atoms-db", default="data/atoms.db")
    ap.add_argument("--index-db", default="data/index.db")
    args = ap.parse_args()

    embedder = OpenAICompatibleEmbedder.from_env(os.environ)
    atoms = SqliteAtomStore(args.atoms_db)
    index = SqliteMemoryIndex(args.index_db)

    newly = IndexingPipeline(atoms, index, embedder).index_session(args.session)
    if newly:
        print(f"indexed {len(newly)} new atom(s) with model={embedder.model}")

    results = MemoryRetriever(embedder, index).query(args.session, args.query, k=args.k)
    print(f"\ntop {len(results)} match(es) for {args.query!r}:")
    for r in results:
        print(f"  {r.score:.3f}  [{r.atom.kind}] {r.atom.text}")


if __name__ == "__main__":
    main()
