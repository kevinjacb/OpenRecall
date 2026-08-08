"""Cursor-based memory extraction pipeline (§F events -> §G atoms).

Reads capture events newer than the per-session extraction cursor, runs the
pluggable :class:`~openrecall_server.memory.extract.Extractor` over each, and writes the
resulting atoms with provenance back to the source event. Advancing the cursor per
event — even one that yields no memories — makes the pass resumable and exactly-once.

Extraction is intentionally a *separate* consumer of the event log, not part of the
gateway's audio hot path: the LLM call is slow, so it runs out of band (a periodic
sweep or a worker) while capture stays real-time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ..events.store import EventStore
from .atom import MemoryAtom
from .extract import Extractor
from .store import AtomStore

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExtractionPipeline:
    def __init__(
        self,
        event_store: EventStore,
        atom_store: AtomStore,
        extractor: Extractor,
        clock: Clock = _utcnow,
    ) -> None:
        self._events = event_store
        self._atoms = atom_store
        self._extractor = extractor
        self._clock = clock

    def run(self, session_id: str) -> list[MemoryAtom]:
        """Extract atoms from every capture event past the cursor; return new atoms."""
        cursor = self._atoms.get_cursor(session_id)
        produced: list[MemoryAtom] = []

        for event in self._events.events(session_id):
            if event.seq <= cursor:
                continue
            for index, memory in enumerate(self._extractor.extract(event.text)):
                atom = MemoryAtom(
                    atom_id=f"{event.event_id}:{index}",
                    session_id=event.session_id,
                    source_event_id=event.event_id,
                    kind=memory.kind,
                    text=memory.text,
                    created_at=self._clock(),
                    occurred_at=event.created_at,  # conversation time (spec D6)
                    start_ms=event.start_ms,
                )
                self._atoms.append(atom)
                produced.append(atom)
            self._atoms.set_cursor(session_id, event.seq)

        return produced
