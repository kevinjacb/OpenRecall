"""Tests for manual speaker correction — name + reassign.

``reassign_speaker`` (scope "all" in v1) relabels every event/atom attributed to
the ``from`` speaker onto the ``to`` speaker, moves the from-speaker's
ring-buffer embeddings into the to-speaker's, and recomputes both centroids
(de-poisoning). ``SpeakerRegistry.name`` sets a display name + bumps enrollment
to confirmed. Both are exercised here via the HTTP-path entry points (the
inbound WS control-frame handlers were retired in favour of HTTP-always).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.ingest.speaker_config import SpeakerConfig
from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.speaker_registry import (
    InMemorySpeakerRegistry,
    Speaker,
    reassign_speaker,
)
from opensapien_server.memory.store import InMemoryAtomStore


def _unit(dim, seed):
    v = [seed] * dim
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _seed(reg, sid, centroid, is_wearer=False):
    reg.add_speaker(Speaker(
        speaker_id=sid, display_name=None, is_wearer=is_wearer,
        enrollment_status="confirmed", centroid=centroid, embedding_model="m",
        dim=4, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))


def test_reassign_all_rel_labels_events_and_atoms_and_moves_embeddings():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "a", _unit(4, 0.5))
    _seed(reg, "b", _unit(4, 0.9))
    # a has 2 confirmed embeddings, b has none
    reg.add_confirmed_embedding("a", _unit(4, 0.5), 0.9)
    reg.add_confirmed_embedding("a", _unit(4, 0.5), 0.9)
    reg.recompute_centroid("a")

    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    for seq in range(3):
        events.append(CaptureEvent(
            event_id=f"s:{seq}", session_id="s", seq=seq, kind="transcript",
            created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), text="x",
            duration_ms=1000, start_ms=seq * 1000, speaker="a",
            speaker_confidence=0.9, speaker_assignment="confirmed"))
    # one event stays attributed to b (not relabeled)
    events.append(CaptureEvent(
        event_id="s:3", session_id="s", seq=3, kind="transcript",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), text="z",
        duration_ms=1000, start_ms=3000, speaker="b",
        speaker_confidence=0.8, speaker_assignment="confirmed"))
    atoms.append(MemoryAtom(
        atom_id="m1", session_id="s", source_event_id="s:0", kind="fact", text="y",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), start_ms=0,
        speaker="a", speaker_confidence=0.9, speaker_assignment="confirmed"))

    reassign_speaker(reg, events, atoms, "a", "b", "all")

    # events relabeled to b (the one already on b stays b)
    assert [e.speaker for e in events.events("s")] == ["b", "b", "b", "b"]
    # atom relabeled to b
    assert atoms.atoms("s")[0].speaker == "b"
    # embeddings moved: a's ring buffer empty, b's has 2
    assert reg.ring_buffer("a") == []
    assert len(reg.ring_buffer("b")) == 2
    # b's centroid recomputed (was None-seeded but now has embeddings)
    assert reg.centroid("b") is not None


def test_name_sets_display_name_and_confirms_enrollment():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id="a", display_name=None, is_wearer=False,
        enrollment_status="implicit", centroid=_unit(4, 0.5),
        embedding_model="m", dim=4, turn_count=0,
        first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))
    reg.name("a", "Sarah")
    s = reg.get("a")
    assert s.display_name == "Sarah"
    assert s.enrollment_status == "confirmed"

def test_reassign_all_sqlite_rel_labels_and_moves_embeddings(tmp_path):
    """The durable backend must not silently diverge from the in-memory one."""
    from opensapien_server.events.store import SqliteEventStore
    from opensapien_server.memory.store import SqliteAtomStore
    from opensapien_server.memory.speaker_registry import SqliteSpeakerRegistry

    reg = SqliteSpeakerRegistry(tmp_path / "speakers.db", SpeakerConfig())
    _seed(reg, "a", _unit(4, 0.5))
    _seed(reg, "b", _unit(4, 0.9))
    reg.add_confirmed_embedding("a", _unit(4, 0.5), 0.9)
    reg.add_confirmed_embedding("a", _unit(4, 0.5), 0.9)
    reg.recompute_centroid("a")

    events = SqliteEventStore(tmp_path / "events.db")
    atoms = SqliteAtomStore(tmp_path / "atoms.db")
    for seq in range(3):
        events.append(CaptureEvent(
            event_id=f"s:{seq}", session_id="s", seq=seq, kind="transcript",
            created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), text="x",
            duration_ms=1000, start_ms=seq * 1000, speaker="a",
            speaker_confidence=0.9, speaker_assignment="confirmed"))
    atoms.append(MemoryAtom(
        atom_id="m1", session_id="s", source_event_id="s:0", kind="fact", text="y",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), start_ms=0,
        speaker="a", speaker_confidence=0.9, speaker_assignment="confirmed"))

    reassign_speaker(reg, events, atoms, "a", "b", "all")

    assert [e.speaker for e in events.events("s")] == ["b", "b", "b"]
    assert atoms.atoms("s")[0].speaker == "b"
    assert reg.ring_buffer("a") == []
    assert len(reg.ring_buffer("b")) == 2
    assert reg.centroid("b") is not None
