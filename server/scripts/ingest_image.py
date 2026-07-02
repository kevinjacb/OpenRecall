#!/usr/bin/env python3
"""Ingest a captured image into memory: store it, caption it, make a scene atom.

The captioned scene becomes a MemoryAtom in the same store as audio memories, so
``query_memory.py`` searches across both. The vision model is configurable — local
or cloud — via env (nothing hardcoded):

    export SENSE_VLM_MODEL=qwen2.5-vl                      # required
    export SENSE_VLM_BASE_URL=http://localhost:11434/v1    # default: Ollama
    export SENSE_VLM_API_KEY=...                           # optional (cloud)
    pip install -e '.[llm]'
    python scripts/ingest_image.py --session <id> --image photo.jpg --at-ms 12000
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sense_server.media.blob import FilesystemBlobStore
from sense_server.memory.store import SqliteAtomStore
from sense_server.vision.model import OpenAICompatibleVisionModel
from sense_server.vision.pipeline import VisionPipeline


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True)
    ap.add_argument("--image", required=True, help="path to a JPEG/PNG image")
    ap.add_argument("--at-ms", type=int, default=0, help="capture offset in the session timeline")
    ap.add_argument("--atoms-db", default="data/atoms.db")
    ap.add_argument("--blobs-dir", default="data/blobs")
    args = ap.parse_args()

    vision = OpenAICompatibleVisionModel.from_env(os.environ)
    image = Path(args.image).read_bytes()
    media_type = "image/png" if args.image.lower().endswith(".png") else "image/jpeg"

    pipe = VisionPipeline(
        blob_store=FilesystemBlobStore(args.blobs_dir),
        vision_model=vision,
        atom_store=SqliteAtomStore(args.atoms_db),
    )
    atom = pipe.capture(args.session, image, captured_at_ms=args.at_ms, media_type=media_type)

    if atom is None:
        print("already captured (same image for this session) — nothing to do")
        return
    print(f"scene atom [{atom.kind}] {atom.text}")
    print(f"  media: {atom.source_event_id}")
    print("run query_memory.py to search across audio + vision memories.")


if __name__ == "__main__":
    main()
