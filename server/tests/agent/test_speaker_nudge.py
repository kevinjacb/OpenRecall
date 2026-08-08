"""Tests for the SpeakerNudgeListener — confirm + name nudges.

The listener fires on SessionCompletion: a confirm nudge for the implicit
"You" once it crosses confirm_turns, and a name nudge for a corroborated but
unnamed unknown once it crosses name_nudge_turns (carrying a
propose={kind:"name_speaker", speaker_id}). Dedupes per speaker_id, respects
the rate limit, and never fires when OPENRECALL_SPEAKER_ENABLED=false.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openrecall_server.agent.speaker_nudge import SpeakerNudgeListener
from openrecall_server.events.model import CaptureEvent
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.ingest.speaker_config import SpeakerConfig
from openrecall_server.memory.extraction_worker import SessionCompletion
from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_proactive(self, *, session_id, request_id, text, atoms, propose=None):
        self.sent.append({
            "session_id": session_id, "request_id": request_id,
            "text": text, "atoms": atoms, "propose": propose,
        })


class _Ids:
    def __init__(self):
        self._c = 0

    def new(self):
        self._c += 1
        return f"id-{self._c}"


class _Clock:
    def now(self):
        return datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def _seed(reg, sid, turns, display=None, enrollment="confirmed", is_wearer=False):
    reg.add_speaker(Speaker(
        speaker_id=sid, display_name=display, is_wearer=is_wearer,
        enrollment_status=enrollment, centroid=[0.5], embedding_model="m", dim=1,
        turn_count=turns, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))


def _completion(sid="s1"):
    return SessionCompletion(sid, datetime(2026, 7, 26, tzinfo=timezone.utc), (0, 9))


def _events_for(sid, n):
    events = InMemoryEventStore()
    for seq in range(n):
        events.append(CaptureEvent(
            event_id=f"s1:{seq}", session_id="s1", seq=seq, kind="transcript",
            created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), text=f"line {seq}",
            duration_ms=1000, start_ms=seq * 1000, speaker=sid,
            speaker_confidence=0.9, speaker_assignment="confirmed"))
    return events


@pytest.mark.asyncio
async def test_name_nudge_fires_when_unnamed_unknown_crosses_threshold():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=8, display=None)  # name_nudge_turns default 8
    events = _events_for("u1", 8)
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, events, ws, SpeakerConfig(enabled=True),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert len(ws.sent) == 1
    assert ws.sent[0]["propose"]["kind"] == "name_speaker"
    assert ws.sent[0]["propose"]["speaker_id"] == "u1"
    # sample lines carried in the text
    assert "line 0" in ws.sent[0]["text"]


@pytest.mark.asyncio
async def test_name_nudge_dedupes_per_speaker():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=8, display=None)
    events = _events_for("u1", 8)
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, events, ws, SpeakerConfig(enabled=True),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    await listener.on_session_completion(_completion())  # second time: deduped
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_confirm_nudge_fires_for_implicit_you():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "you", turns=10, display="You", enrollment="implicit", is_wearer=True)
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, InMemoryEventStore(), ws,
                                    SpeakerConfig(enabled=True, confirm_turns=10),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert len(ws.sent) == 1
    assert ws.sent[0]["propose"] is None
    assert "you" in ws.sent[0]["text"].lower() or "main voice" in ws.sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_no_nudge_when_speaker_disabled():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=8, display=None)
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, InMemoryEventStore(), ws,
                                    SpeakerConfig(enabled=False),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert ws.sent == []


@pytest.mark.asyncio
async def test_no_nudge_when_below_turn_threshold():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=7, display=None)  # below name_nudge_turns=8
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, _events_for("u1", 7), ws, SpeakerConfig(enabled=True),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert ws.sent == []


@pytest.mark.asyncio
async def test_no_nudge_when_already_named():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=8, display="Sarah")  # already named
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, _events_for("u1", 8), ws, SpeakerConfig(enabled=True),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert ws.sent == []


@pytest.mark.asyncio
async def test_rate_limit_caps_sends_per_minute():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    # three distinct unnamed unknowns each crossing the threshold
    for i in range(3):
        _seed(reg, f"u{i}", turns=8, display=None)
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, InMemoryEventStore(), ws, SpeakerConfig(enabled=True),
                                    rate_limit_per_min=2, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    # only 2 of the 3 fire (rate limit); the 3rd is held back this minute
    assert len(ws.sent) == 2