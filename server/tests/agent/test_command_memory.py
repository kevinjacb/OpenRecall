from datetime import datetime

from openrecall_server.agent.command_memory import CommandMemoryWriter
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.contracts.clock import FakeClock


def test_on_ack_writes_command_event_atom():
    store = InMemoryAtomStore()
    w = CommandMemoryWriter(store=store, clock=FakeClock(start=datetime.fromisoformat("2026-08-25T00:00:00+00:00")))
    w.on_ack(command_id="cmd-1", session_id="s", command_type="capture_photo",
             source_event_id="s:7", trigger_text="take a photo of mine")
    atoms = store.list(session_id="s", limit=10)[0]
    assert len(atoms) == 1
    a = atoms[0]
    assert a.atom_id == "cmd-1"  # command_id is the idempotency key
    assert a.kind == "command_event"
    assert a.source_pipeline_version == "command"
    assert a.source_event_id == "s:7"
    assert a.session_id == "s"
    assert "capture_photo" not in a.text  # human text, not the raw type
    assert "photo" in a.text.lower()
    assert "take a photo of mine" in a.text


def test_on_ack_is_idempotent_on_repeat():
    store = InMemoryAtomStore()
    w = CommandMemoryWriter(store=store, clock=FakeClock(start=datetime.fromisoformat("2026-08-25T00:00:00+00:00")))
    w.on_ack("cmd-1", "s", "capture_photo", "s:7", "take a photo")
    w.on_ack("cmd-1", "s", "capture_photo", "s:7", "take a photo")  # re-ack
    assert len(store.list(session_id="s", limit=10)[0]) == 1


def test_on_ack_skips_when_no_provenance():
    # A /agent-issued command has no transcript provenance -> no command memory.
    store = InMemoryAtomStore()
    w = CommandMemoryWriter(store=store, clock=FakeClock(start=datetime.fromisoformat("2026-08-25T00:00:00+00:00")))
    w.on_ack("cmd-9", "s", "capture_photo", source_event_id=None, trigger_text=None)
    assert store.list(session_id="s", limit=10)[0] == []
