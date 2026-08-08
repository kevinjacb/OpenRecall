"""Phase 2 — auto-titling and the idle sweep (spec §2.1, §2.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.sessions.segment_meta import InMemorySegmentMetaStore
from opensapien_server.sessions.segments import SEGMENT_IDLE_MS, SegmentIndex
from opensapien_server.sessions.sweeper import SegmentSweeper
from opensapien_server.sessions.titler import SegmentTitler

_T0 = datetime(2026, 8, 8, 9, 0, 0, tzinfo=timezone.utc)


class FakeLLM:
    """Stands in for OpenAICompatibleChatModel — same `complete(system, user)`."""

    def __init__(self, reply="Studio standup", raises=False):
        self.reply = reply
        self.raises = raises
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        if self.raises:
            raise RuntimeError("model is down")
        return self.reply


def _event(seq, *, session_id="s1", at=None, text="let's start the standup"):
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=at if at is not None else _T0 + timedelta(seconds=seq),
        text=text,
        duration_ms=1000,
        start_ms=seq * 1000,
    )


def _fixture(llm=None, events=None):
    store = InMemoryEventStore()
    for e in events or [_event(0), _event(1)]:
        store.append(e)
    index = SegmentIndex()
    for e in store.events("s1"):
        index.record(e)
    meta = InMemorySegmentMetaStore()
    titler = SegmentTitler(events=store, meta=meta, llm_chat=llm)
    return store, index, meta, titler


# ---- titling ----------------------------------------------------------------


def test_titles_a_closed_segment_from_its_transcript():
    llm = FakeLLM("Studio standup")
    _store, index, meta, titler = _fixture(llm)
    segment = index.get("s1:0")

    assert titler.title_segment(segment) == "Studio standup"

    assert meta.get("s1:0").title == "Studio standup"
    assert meta.get("s1:0").title_source == "llm"
    assert "let's start the standup" in llm.calls[0]


def test_no_llm_means_no_title_and_no_error():
    """Every deployment without an LLM keeps working; the client falls back
    to `preview`."""
    _store, index, meta, titler = _fixture(llm=None)

    assert titler.title_segment(index.get("s1:0")) is None
    assert meta.get("s1:0") is None


def test_an_llm_failure_is_swallowed():
    _store, index, meta, titler = _fixture(FakeLLM(raises=True))

    assert titler.title_segment(index.get("s1:0")) is None
    assert meta.get("s1:0") is None


def test_a_user_title_is_never_replaced():
    llm = FakeLLM("Auto name")
    _store, index, meta, titler = _fixture(llm)
    meta.set_title("s1:0", "My name", source="user")

    assert titler.title_segment(index.get("s1:0")) is None

    assert meta.get("s1:0").title == "My name"
    assert llm.calls == []  # and it didn't even pay for the call


def test_an_existing_title_is_not_regenerated():
    llm = FakeLLM("Second guess")
    _store, index, meta, titler = _fixture(llm)
    meta.set_title("s1:0", "First guess", source="llm")

    assert titler.title_segment(index.get("s1:0")) is None
    assert llm.calls == []


def test_a_chatty_model_reply_is_normalised():
    """Instruction-following is not guaranteed, so a preamble or a quoted,
    multi-line answer must not become a recording's name verbatim."""
    llm = FakeLLM('"Studio standup".\nHope that helps!')
    _store, index, meta, titler = _fixture(llm)

    assert titler.title_segment(index.get("s1:0")) == "Studio standup"


def test_an_empty_reply_is_rejected():
    _store, index, meta, titler = _fixture(FakeLLM("   "))

    assert titler.title_segment(index.get("s1:0")) is None
    assert meta.get("s1:0") is None


def test_a_silent_segment_is_not_titled():
    llm = FakeLLM()
    _store, index, meta, titler = _fixture(llm, events=[_event(0, text="   ")])

    assert titler.title_segment(index.get("s1:0")) is None
    assert llm.calls == []


def test_only_this_segments_transcript_is_sent():
    """A title generated from the neighbouring recording's speech would be
    confidently wrong."""
    llm = FakeLLM()
    events = [
        _event(0, at=_T0, text="morning standup"),
        _event(1, at=_T0 + timedelta(milliseconds=SEGMENT_IDLE_MS + 1),
               text="afternoon retro"),
    ]
    _store, index, _meta, titler = _fixture(llm, events=events)

    titler.title_segment(index.get("s1:1"))

    assert "afternoon retro" in llm.calls[0]
    assert "morning standup" not in llm.calls[0]


# ---- the sweep --------------------------------------------------------------


def test_sweep_closes_idle_segments_and_titles_them():
    llm = FakeLLM("Studio standup")
    _store, index, meta, titler = _fixture(llm)
    # Age the segment past the idle threshold.
    index.get("s1:0").ended_at = datetime.now(timezone.utc) - timedelta(
        milliseconds=SEGMENT_IDLE_MS + 1000,
    )
    sweeper = SegmentSweeper(index=index, titler=titler)

    assert sweeper.sweep_once() == 1

    assert index.get("s1:0").closed is True
    assert meta.get("s1:0").title == "Studio standup"


def test_sweep_leaves_a_live_segment_alone():
    _store, index, meta, titler = _fixture(FakeLLM())
    index.get("s1:0").ended_at = datetime.now(timezone.utc)
    sweeper = SegmentSweeper(index=index, titler=titler)

    assert sweeper.sweep_once() == 0
    assert index.get("s1:0").closed is False


def test_sweep_without_a_titler_still_closes():
    _store, index, _meta, _titler = _fixture()
    index.get("s1:0").ended_at = datetime.now(timezone.utc) - timedelta(
        milliseconds=SEGMENT_IDLE_MS + 1000,
    )

    assert SegmentSweeper(index=index).sweep_once() == 1
    assert index.get("s1:0").closed is True


def test_a_titler_that_explodes_does_not_stop_the_sweep():
    class Exploding:
        def title_segment(self, segment):
            raise RuntimeError("boom")

    _store, index, _meta, _titler = _fixture()
    index.get("s1:0").ended_at = datetime.now(timezone.utc) - timedelta(
        milliseconds=SEGMENT_IDLE_MS + 1000,
    )
    sweeper = SegmentSweeper(index=index, titler=Exploding())

    assert sweeper.sweep_once() == 1
    assert index.get("s1:0").closed is True
