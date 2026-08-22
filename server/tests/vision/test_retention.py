from datetime import datetime, timezone, timedelta

from openrecall_server.media.blob import InMemoryBlobStore
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.vision.retention import sweep_vision_retention


def _scene(session_id, digest, occurred_at):
    return MemoryAtom(
        atom_id=f"{session_id}:scene:{digest}",
        session_id=session_id, source_event_id=f"blob:{digest}",
        kind="scene", text="x", created_at=occurred_at,
        source_pipeline_version="vision",
        occurred_at=occurred_at, start_ms=0,
    )


def test_sweep_deletes_old_blobs_keeps_atoms():
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    old_digest = blobs.put(b"old-img")
    new_digest = blobs.put(b"new-img")
    atoms.append(_scene("s1", old_digest, now - timedelta(days=40)))
    atoms.append(_scene("s1", new_digest, now - timedelta(days=1)))
    deleted = sweep_vision_retention(atoms, blobs, snapshot_days=30, now=now)
    assert deleted == 1
    assert not blobs.has(old_digest)   # old image swept
    assert blobs.has(new_digest)        # new image kept
    # atoms kept forever (the caption is the product)
    scenes = [a for a in atoms.iter_atoms() if a.kind == "scene"]
    assert len(scenes) == 2


def test_sweep_zero_days_keeps_everything():
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    d = blobs.put(b"img")
    atoms.append(_scene("s1", d, now - timedelta(days=9999)))
    assert sweep_vision_retention(atoms, blobs, snapshot_days=0, now=now) == 0
    assert blobs.has(d)


def test_sweep_ignores_non_scene_atoms():
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    d = blobs.put(b"img")
    # a transcript atom whose source_event_id happens to be blob:... must NOT be swept
    atoms.append(MemoryAtom(
        atom_id="s1:t:1", session_id="s1", source_event_id=f"blob:{d}",
        kind="fact", text="x", created_at=now - timedelta(days=9999),
        occurred_at=now - timedelta(days=9999), start_ms=0,
    ))
    assert sweep_vision_retention(atoms, blobs, snapshot_days=30, now=now) == 0
    assert blobs.has(d)


def test_sweep_skips_scene_atoms_with_missing_blob():
    # a scene atom whose blob was already deleted (or never stored) is skipped, not an error
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    atoms.append(_scene("s1", "deadbeef", now - timedelta(days=40)))
    assert sweep_vision_retention(atoms, blobs, snapshot_days=30, now=now) == 0