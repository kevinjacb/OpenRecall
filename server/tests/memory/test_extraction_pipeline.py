"""Tests for the cursor-based extraction pipeline.

The pipeline reads new capture events from the §F event store, runs the (pluggable,
model-agnostic) extractor over each, and writes the resulting §G atoms with
provenance. A per-session cursor makes it resumable and exactly-once: re-running
processes only events past the cursor, and an event that yields zero memories still
advances the cursor so it is never re-extracted.
"""

from datetime import datetime, timezone

from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.memory.extract import ExtractedMemory
from opensapien_server.memory.pipeline import ExtractionPipeline
from opensapien_server.memory.store import InMemoryAtomStore

FIXED = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeExtractor:
    def __init__(self, mapping: dict[str, list[ExtractedMemory]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def extract(self, text: str) -> list[ExtractedMemory]:
        self.calls.append(text)
        return self.mapping.get(text, [])


def event(session_id: str, seq: int, text: str) -> CaptureEvent:
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=FIXED,
        text=text,
        duration_ms=5000,
        start_ms=seq * 5000,
    )


def build(extractor):
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    pipe = ExtractionPipeline(events, atoms, extractor, clock=lambda: FIXED)
    return events, atoms, pipe


def test_new_events_become_atoms_with_provenance_and_advance_the_cursor():
    ex = FakeExtractor(
        {
            "I like tea": [ExtractedMemory(kind="preference", text="Kevin likes tea")],
            "meet Bob at 5": [ExtractedMemory(kind="task", text="Kevin to meet Bob at 5")],
        }
    )
    events, atoms, pipe = build(ex)
    events.append(event("s1", 0, "I like tea"))
    events.append(event("s1", 1, "meet Bob at 5"))

    produced = pipe.run("s1")

    stored = atoms.atoms("s1")
    assert [a.text for a in stored] == ["Kevin likes tea", "Kevin to meet Bob at 5"]
    assert [a.kind for a in stored] == ["preference", "task"]
    assert stored[0].source_event_id == "s1:0"  # provenance
    assert stored[0].start_ms == 0 and stored[1].start_ms == 5000  # timeline
    assert stored[0].atom_id == "s1:0:0"  # deterministic id
    assert [a.text for a in produced] == ["Kevin likes tea", "Kevin to meet Bob at 5"]
    assert atoms.get_cursor("s1") == 1


def test_rerun_is_idempotent_and_processes_only_new_events():
    ex = FakeExtractor({"a": [ExtractedMemory(kind="fact", text="A")]})
    events, atoms, pipe = build(ex)
    events.append(event("s1", 0, "a"))

    assert len(pipe.run("s1")) == 1
    assert pipe.run("s1") == []  # nothing new
    assert ex.calls == ["a"]  # extractor not called again for the same event

    events.append(event("s1", 1, "a"))  # a new event with the same text
    produced = pipe.run("s1")
    assert [a.atom_id for a in produced] == ["s1:1:0"]
    assert len(atoms.atoms("s1")) == 2


def test_event_yielding_no_memories_still_advances_the_cursor():
    ex = FakeExtractor({})  # everything yields []
    events, atoms, pipe = build(ex)
    events.append(event("s1", 0, "nothing notable"))

    assert pipe.run("s1") == []
    assert atoms.get_cursor("s1") == 0  # advanced despite zero atoms

    pipe.run("s1")
    assert ex.calls == ["nothing notable"]  # not re-extracted on the next run
