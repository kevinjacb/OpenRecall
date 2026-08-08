"""Tests for the GatewayCore.send_proactive seam + the per-connection
outbox drain loop that the adapter wires up.

P3: the engine calls ``send_proactive`` on the per-connection
GatewayCore. The core enqueues a ProactiveMessage into its
ProactiveOutbox and signals an event. The adapter's proactive_task
drains the outbox as JSON frames and sends them on the open
WebSocket. This file exercises both halves without touching real
sockets.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from openrecall_server.agent.metrics import InMemoryMetricsRecorder
from openrecall_server.contracts.clock import FakeClock
from openrecall_server.gateway.core import GatewayCore, ProactiveOutbox
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler
from openrecall_server.protocol.messages import ProactiveMessage


class _FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class _FakeTranscriber:
    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return ""


def make_core(outbox: ProactiveOutbox | None = None) -> GatewayCore:
    def factory(start_seq: int) -> AudioIngestPipeline:
        return AudioIngestPipeline(
            reassembler=SessionReassembler(start_seq=start_seq),
            decoder=_FakeDecoder(),
            transcriber=_FakeTranscriber(),
            hop_ms=20,
            window_ms=100,
            sample_rate=16000,
        )

    return GatewayCore(pipeline_factory=factory, proactive_outbox=outbox)


async def test_send_proactive_enqueues_into_outbox():
    """send_proactive is the WsSender seam; the message lands in the
    per-connection outbox and the event is signalled so the drain
    task wakes."""
    outbox = ProactiveOutbox(ttl_s=30.0, clock=FakeClock())
    core = make_core(outbox=outbox)

    await core.send_proactive(
        session_id="s1",
        request_id="r1",
        text="hi",
        atoms=("a1",),
    )

    drained = outbox.drain("s1")
    assert len(drained) == 1
    msg = drained[0]
    assert msg.session_id == "s1"
    assert msg.request_id == "r1"
    assert msg.text == "hi"
    assert msg.atoms == ("a1",)
    # ProactiveMessage serialises the way the wire expects it.
    assert msg.model_dump()["type"] == "proactive"


async def test_send_proactive_raises_without_outbox():
    """If the gateway forgot to wire an outbox, send_proactive must
    NOT silently drop — the engine's contract is best-effort delivery
    to an open WS, not a generic log line."""
    core = make_core(outbox=None)
    with pytest.raises(RuntimeError, match="no ProactiveOutbox"):
        await core.send_proactive(
            session_id="s1",
            request_id="r1",
            text="hi",
            atoms=(),
        )


async def test_drain_loop_picks_up_enqueued_messages():
    """A minimal drain loop (mirroring the adapter's proactive_task):
    wait for the outbox event, drain, send to a stub WS, repeat until
    the connection closes. Proves the wakeup/signalling handshake
    works without a real socket."""
    outbox = ProactiveOutbox(ttl_s=30.0, clock=FakeClock())
    core = make_core(outbox=outbox)

    sent: list[ProactiveMessage] = []
    closed = asyncio.Event()

    async def fake_send(msg: ProactiveMessage) -> None:
        sent.append(msg)

    async def drain_loop() -> None:
        # Bound the loop with a timeout so a regression never hangs CI.
        async with asyncio.timeout(2.0):
            while not closed.is_set():
                await outbox.wait()
                for m in outbox.drain("s1"):
                    await fake_send(m)

    task = asyncio.create_task(drain_loop())
    # Give the task a chance to enter wait().
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await core.send_proactive(
        session_id="s1",
        request_id="r1",
        text="first",
        atoms=(),
    )
    # Let the loop wake, drain, and append.
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(sent) == 1
    assert sent[0].text == "first"

    await core.send_proactive(
        session_id="s1",
        request_id="r2",
        text="second",
        atoms=("a1", "a2"),
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(sent) == 2
    assert sent[1].atoms == ("a1", "a2")

    # Closing the loop: set the close flag and poke the outbox so the
    # wait() that's blocked inside the loop wakes and observes the flag.
    closed.set()
    outbox.signal()
    await asyncio.wait_for(task, timeout=2.0)


def test_proactive_message_round_trip_json():
    """The wire form must carry the four fields the relay dispatches on.
    Belt-and-suspenders guard against accidentally renaming a field
    after the phone has been built against the old schema."""
    msg = ProactiveMessage(
        session_id="s1", request_id="r1", text="hi", atoms=("a1",),
    )
    payload = json.loads(msg.model_dump_json())
    assert payload == {
        "type": "proactive",
        "session_id": "s1",
        "request_id": "r1",
        "text": "hi",
        "atoms": ["a1"],
        "propose": None,
    }


def test_outbox_drain_after_send_matches_proactive_message():
    """The message the engine sent through send_proactive is the same
    object class the relay forwards verbatim — proves the GatewayCore
    -> adapter handoff is a single type with no translation step."""
    outbox = ProactiveOutbox(
        ttl_s=30.0,
        clock=FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)),
        metrics=InMemoryMetricsRecorder(),
    )
    core = make_core(outbox=outbox)
    # Send synchronously via the core; .send_proactive is async because
    # the engine awaits it, but the enqueue itself is sync.
    asyncio.run(
        core.send_proactive(session_id="s1", request_id="r1", text="hi", atoms=())
    )
    drained = outbox.drain("s1")
    assert len(drained) == 1
    assert isinstance(drained[0], ProactiveMessage)
