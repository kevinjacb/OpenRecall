"""Tests for the speaker fields carried on Transcript/CaptureEvent/MemoryAtom.

All three are additive nullable fields with ``None`` defaults so every existing
call site keeps working; only the speaker path populates them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sense_server.ingest.transcriber import Transcript
from sense_server.events.model import CaptureEvent
from sense_server.memory.atom import MemoryAtom


def test_transcript_defaults_speaker_to_none():
    t = Transcript(text="hi", duration_ms=1000)
    assert t.speaker is None
    assert t.speaker_confidence is None
    assert t.speaker_assignment is None


def test_transcript_carries_speaker_fields():
    t = Transcript(text="hi", duration_ms=1000, speaker="uuid-1",
                   speaker_confidence=0.82, speaker_assignment="confirmed")
    assert t.speaker == "uuid-1"
    assert t.speaker_confidence == 0.82
    assert t.speaker_assignment == "confirmed"


def test_capture_event_defaults_speaker_to_none():
    e = CaptureEvent(event_id="e1", session_id="s1", seq=0, kind="transcript",
                     created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
                     text="hi", duration_ms=1000, start_ms=0)
    assert e.speaker is None
    assert e.speaker_confidence is None
    assert e.speaker_assignment is None


def test_capture_event_carries_speaker_fields():
    e = CaptureEvent(event_id="e1", session_id="s1", seq=0, kind="transcript",
                     created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
                     text="hi", duration_ms=1000, start_ms=0,
                     speaker="uuid-1", speaker_confidence=0.82,
                     speaker_assignment="confirmed")
    assert e.speaker == "uuid-1"
    assert e.speaker_assignment == "confirmed"


def test_memory_atom_defaults_speaker_to_none():
    a = MemoryAtom(atom_id="a1", session_id="s1", source_event_id="e1",
                   kind="fact", text="x",
                   created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
                   start_ms=0)
    assert a.speaker is None
    assert a.speaker_confidence is None
    assert a.speaker_assignment is None


def test_memory_atom_carries_speaker_fields():
    a = MemoryAtom(atom_id="a1", session_id="s1", source_event_id="e1",
                   kind="fact", text="x",
                   created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
                   start_ms=0, speaker="uuid-1", speaker_confidence=0.8,
                   speaker_assignment="tentative")
    assert a.speaker == "uuid-1"
    assert a.speaker_assignment == "tentative"