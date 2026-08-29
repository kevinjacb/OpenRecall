"""One-shot cleanup: delete speaker rows minted by the FakeSpeakerEmbedder.

The 2026-08-29 incident: with OPENRECALL_SPEAKER_EMBED_MODEL unset, the
gateway silently fingerprinted with the 16-dim test fake and minted hundreds
of garbage speaker rows (dim=16, embedding_model="unknown"). Real Resemblyzer
rows are dim=256 / model="resemblyzer". This tool deletes the fakes and keeps
everything real — including named speakers, which are never touched unless
they are fake-dimensioned AND you pass --include-named.

Usage (gateway stopped, or at least idle):
    .venv/bin/python tools/prune_fake_speakers.py            # dry run
    .venv/bin/python tools/prune_fake_speakers.py --apply    # delete
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

REAL_DIM = 256


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/speakers.db")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--include-named", action="store_true",
                    help="also delete NAMED fake-dim rows (default: keep and warn)")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    rows = list(db.execute(
        "SELECT speaker_id, display_name, dim, embedding_model, turn_count FROM speakers"
    ))
    fakes = [r for r in rows if r[2] != REAL_DIM]
    named_fakes = [r for r in fakes if r[1]]
    to_delete = fakes if args.include_named else [r for r in fakes if not r[1]]

    print(f"{len(rows)} speakers total; {len(fakes)} fake-dim (dim != {REAL_DIM}); "
          f"{len(to_delete)} to delete")
    for r in named_fakes:
        kept = "DELETING" if args.include_named else "KEEPING (named)"
        print(f"  named fake-dim row: {r[1]!r} (dim={r[2]}, turns={r[4]}) — {kept}")

    if not args.apply:
        print("dry run — pass --apply to delete")
        return 0
    ids = [r[0] for r in to_delete]
    for sid in ids:
        db.execute("DELETE FROM speaker_embeddings WHERE speaker_id=?", (sid,))
        db.execute("DELETE FROM speakers WHERE speaker_id=?", (sid,))
    db.commit()
    print(f"deleted {len(ids)} speakers (+ their ring-buffer embeddings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
