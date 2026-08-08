"""Integration: audio and vision memories share one index and one retriever.

Vision "scene" atoms are ordinary MemoryAtoms in the same AtomStore as audio-derived
atoms, so the existing IndexingPipeline + MemoryRetriever index and search across
both modalities with no special-casing. This pins that multimodal contract.
"""

from datetime import datetime, timezone

from openrecall_server.media.blob import InMemoryBlobStore
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.retrieval import IndexingPipeline, MemoryRetriever
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.vision.pipeline import VisionPipeline

FIXED = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeVision:
    def caption(self, image: bytes, *, media_type: str = "image/jpeg") -> str:
        return "a neon sign reading TEA HOUSE"


class FakeEmbedder:
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.mapping[t] for t in texts]


def test_audio_and_vision_atoms_are_retrieved_from_one_index():
    atoms = InMemoryAtomStore()
    # an audio-derived memory (as produced by the extraction pipeline)
    atoms.append(
        MemoryAtom(
            atom_id="s1:0:0",
            session_id="s1",
            source_event_id="s1:0",
            kind="fact",
            text="Kevin likes tea",
            created_at=FIXED,
            start_ms=0,
        )
    )
    # a vision-derived memory (via the vision pipeline) into the SAME store
    VisionPipeline(InMemoryBlobStore(), FakeVision(), atoms, clock=lambda: FIXED).capture(
        "s1", b"jpeg-bytes", captured_at_ms=5000
    )

    embedder = FakeEmbedder(
        {
            "Kevin likes tea": [1.0, 0.0],
            "a neon sign reading TEA HOUSE": [0.0, 1.0],
            "what sign did I see?": [0.0, 1.0],
        }
    )
    index = InMemoryMemoryIndex()
    IndexingPipeline(atoms, index, embedder).index_session("s1")

    results = MemoryRetriever(embedder, index).query("s1", "what sign did I see?", k=2)

    kinds = {r.atom.kind for r in results}
    assert kinds == {"scene", "fact"}  # both modalities live in one index
    assert results[0].atom.kind == "scene"  # the visual memory matches the visual query
