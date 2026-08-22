"""Scene image-blob retention (P3, D8 extended).

Scene *atoms* (the caption text) are kept forever — the derived text is the
product. The bulky/sensitive *image blob* is swept once it is older than
``retention.snapshot_days``. A scene atom's ``source_event_id`` is
``"blob:{digest}"``; deleting the blob leaves the atom referencing a missing
image (a future ``GET /media/blob/{digest}`` 404s), which is the intended
tiered policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..media.blob import BlobStore
from ..memory.store import AtomStore


def sweep_vision_retention(
    atom_store: AtomStore, blob_store: BlobStore, *,
    snapshot_days: int, now: datetime,
) -> int:
    if snapshot_days <= 0:
        return 0
    cutoff = now - timedelta(days=snapshot_days)
    deleted = 0
    for atom in atom_store.iter_atoms():
        if atom.kind != "scene":
            continue
        if atom.occurred_at is None or atom.occurred_at >= cutoff:
            continue
        src = atom.source_event_id or ""
        if src.startswith("blob:"):
            digest = src[len("blob:"):]
            if blob_store.has(digest):
                blob_store.delete(digest)
                deleted += 1
    return deleted