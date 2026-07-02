"""Tests for the vision capture pipeline.

A captured image is stored content-addressed, captioned by the (pluggable) vision
model, and turned into a "scene" MemoryAtom written to the *same* AtomStore audio
uses — so vision and audio memories share one index and one retriever. Capturing the
same image for a session again is a no-op (and does not re-call the costly model).
"""

from datetime import datetime, timezone

from sense_server.media.blob import InMemoryBlobStore, sha256_hex
from sense_server.memory.store import InMemoryAtomStore
from sense_server.vision.pipeline import VisionPipeline

FIXED = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeVision:
    def __init__(self, mapping: dict[bytes, str]) -> None:
        self.mapping = mapping
        self.calls = 0

    def caption(self, image: bytes, *, media_type: str = "image/jpeg") -> str:
        self.calls += 1
        return self.mapping[image]


def build(mapping):
    blobs = InMemoryBlobStore()
    vision = FakeVision(mapping)
    atoms = InMemoryAtomStore()
    pipe = VisionPipeline(blobs, vision, atoms, clock=lambda: FIXED)
    return blobs, vision, atoms, pipe


def test_capture_stores_blob_captions_and_creates_a_scene_atom():
    blobs, _vision, atoms, pipe = build({b"img1": "a cafe sign reading OPEN"})

    atom = pipe.capture("s1", b"img1", captured_at_ms=12000)

    assert atom is not None
    assert atom.kind == "scene"
    assert atom.text == "a cafe sign reading OPEN"
    assert atom.start_ms == 12000
    assert atom.source_event_id == "blob:" + sha256_hex(b"img1")  # provenance to media
    assert blobs.has(sha256_hex(b"img1"))  # image kept, content-addressed
    assert atoms.atoms("s1") == [atom]  # lands in the shared atom store


def test_recapturing_the_same_image_is_a_noop_and_does_not_recall_the_model():
    _blobs, vision, atoms, pipe = build({b"img1": "a dog"})

    pipe.capture("s1", b"img1", captured_at_ms=0)
    again = pipe.capture("s1", b"img1", captured_at_ms=99000)

    assert again is None
    assert vision.calls == 1
    assert len(atoms.atoms("s1")) == 1


def test_same_image_in_a_different_session_gets_its_own_atom():
    _blobs, vision, _atoms, pipe = build({b"img1": "a bridge"})

    a1 = pipe.capture("s1", b"img1", captured_at_ms=0)
    a2 = pipe.capture("s2", b"img1", captured_at_ms=0)

    assert a1 is not None and a2 is not None
    assert {a1.session_id, a2.session_id} == {"s1", "s2"}
    assert vision.calls == 2
