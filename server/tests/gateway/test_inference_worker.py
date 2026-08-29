"""Tests for the InferenceWorker: the off-event-loop inference thread.

S1 decouples ASR inference from the WS receive loop. One daemon thread fed
by a bounded ``queue.Queue`` processes ``handle_message`` calls off the event
loop; results are posted back via ``call_soon_threadsafe``. These tests pin
the five contract points:

(a) the event loop is NOT blocked while inference runs,
(b) overflow drops the oldest inbound frame (last resort; opens a chunk_seq hole),
(c) replies are delivered in enqueue order,
(d) ``GatewayError`` propagates to the sink (so the outer handler closes 1002),
(e) any other exception is logged + skipped (the link stays up).

Plus R4: two concurrent ``transcribe`` calls on the same inference lock are
serialized (no concurrent model.generate).
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from openrecall_server.gateway.core import GatewayError
from openrecall_server.gateway.inference_worker import InferenceWorker


# --- (a) loop not blocked during inference -----------------------------------


@pytest.mark.asyncio
async def test_loop_not_blocked_during_inference():
    """A concurrent coroutine completes while a slow handle_message runs
    on the worker — the event loop is not blocked."""
    processed: list[bytes] = []

    def fake_handle(core, msg):
        time.sleep(0.2)
        processed.append(msg)
        return []

    loop = asyncio.get_running_loop()
    results: asyncio.Queue = asyncio.Queue()
    worker = InferenceWorker(
        fake_handle, core=None, loop=loop, sink=results.put_nowait, max_queue=4,
    )
    worker.start()
    try:
        worker.enqueue(b"a")
        # If the loop were blocked (synchronous to_thread), this 0-second
        # sleep would not complete until inference finishes (0.2s). With the
        # worker, the loop is free and this completes immediately.
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.05)
        # Inference is still running — not done yet.
        assert len(processed) <= 1
    finally:
        await worker.stop()
    # After stop, the single message was processed.
    assert processed == [b"a"]


# --- (b) overflow drops the oldest inbound frame (last resort) ---------------


def test_default_queue_depth_is_32():
    """Each queued frame is a real audio packet (dropping one opens a
    chunk_seq hole), so the default depth must be generous — 32 packets
    (~6.4 s) absorbs replay bursts and slow hops."""
    loop = asyncio.new_event_loop()
    try:
        worker = InferenceWorker(
            lambda core, msg: [], core=None, loop=loop, sink=lambda _: None,
        )
        assert worker._q.maxsize == 32
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_overflow_drops_oldest():
    """When the queue is full, the OLDEST unprocessed frame is dropped (not
    the newest) — keeps the freshest audio; the dropped packet's chunk_seq
    hole is repaired by the backfill path."""
    processed: list[bytes] = []
    started = threading.Event()
    block = threading.Event()

    def fake_handle(core, msg):
        started.set()
        block.wait(timeout=2.0)
        processed.append(msg)
        return []

    loop = asyncio.get_running_loop()
    worker = InferenceWorker(
        fake_handle, core=None, loop=loop, sink=lambda _: None, max_queue=2,
    )
    worker.start()
    try:
        worker.enqueue(b"a")
        # Wait until the worker picks up "a" (blocking on the event).
        assert started.wait(timeout=1.0), "worker did not start processing"
        # Queue is now empty (worker holds "a"). Fill it to capacity.
        worker.enqueue(b"b")
        worker.enqueue(b"c")
        assert worker._q.qsize() == 2  # full
        # Enqueue one more — drops the oldest ("b").
        worker.enqueue(b"d")
        assert worker._q.qsize() == 2  # still full, not over capacity
        block.set()  # unblock the worker
        # Wait for the worker to process all remaining messages ("a", "c",
        # "d") before stopping — otherwise stop() may race the drain.
        for _ in range(200):
            if len(processed) >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        block.set()
        await worker.stop()

    # "a" was being processed; "b" was dropped; "c" and "d" were processed.
    assert processed == [b"a", b"c", b"d"]


# --- (c) in-order replies ----------------------------------------------------


@pytest.mark.asyncio
async def test_in_order_replies():
    """Replies are delivered in the order messages were enqueued, even when
    processing times vary (the worker is a single-threaded FIFO)."""
    delays = {b"a": 0.06, b"b": 0.01, b"c": 0.03}

    def fake_handle(core, msg):
        time.sleep(delays.get(msg, 0))
        return [msg.decode()]

    loop = asyncio.get_running_loop()
    results: asyncio.Queue = asyncio.Queue()
    worker = InferenceWorker(
        fake_handle, core=None, loop=loop, sink=results.put_nowait, max_queue=4,
    )
    worker.start()
    try:
        worker.enqueue(b"a")
        worker.enqueue(b"b")
        worker.enqueue(b"c")
        collected = []
        for _ in range(3):
            item = await asyncio.wait_for(results.get(), timeout=2.0)
            collected.append(item)
    finally:
        await worker.stop()

    assert collected == [["a"], ["b"], ["c"]]


# --- (d) GatewayError propagates to the sink ---------------------------------


@pytest.mark.asyncio
async def test_gateway_error_propagates_to_sink():
    """A GatewayError from handle_message is posted to the sink so the
    reply-sender can re-raise it (→ outer handler closes 1002)."""

    def fake_handle(core, msg):
        raise GatewayError("bad protocol frame")

    loop = asyncio.get_running_loop()
    results: asyncio.Queue = asyncio.Queue()
    worker = InferenceWorker(
        fake_handle, core=None, loop=loop, sink=results.put_nowait, max_queue=4,
    )
    worker.start()
    try:
        worker.enqueue(b"a")
        item = await asyncio.wait_for(results.get(), timeout=2.0)
        assert isinstance(item, GatewayError)
        assert "bad protocol frame" in str(item)
    finally:
        await worker.stop()


# --- (e) other exceptions log + skip (link stays up) -------------------------


@pytest.mark.asyncio
async def test_other_exceptions_log_and_skip(caplog):
    """A non-GatewayError exception is logged; the worker continues
    processing subsequent messages (the link stays up)."""
    call_count = 0

    def fake_handle(core, msg):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("transcribe blew up")
        return ["ok"]

    loop = asyncio.get_running_loop()
    results: asyncio.Queue = asyncio.Queue()
    worker = InferenceWorker(
        fake_handle, core=None, loop=loop, sink=results.put_nowait, max_queue=4,
    )
    worker.start()
    try:
        worker.enqueue(b"a")  # raises ValueError — skipped
        worker.enqueue(b"b")  # succeeds
        item = await asyncio.wait_for(results.get(), timeout=2.0)
        # Only the second message's reply was posted; the first was skipped.
        assert item == ["ok"]
    finally:
        await worker.stop()

    # The ValueError was logged by the worker.
    assert any(
        "handle_message failed" in r.message for r in caplog.records
    ), "expected 'handle_message failed' log line not found"


# --- R4: inference lock serializes concurrent transcribe calls ---------------


def test_whisper_concurrent_transcribe_serialized_by_inference_lock():
    """R4: two concurrent transcribe calls sharing a threading.Lock are
    serialized — the second call does not enter model.generate while the
    first is still running. This protects reconnect-overlap and multi-
    connection concurrent model calls."""
    from openrecall_server.ingest.whisper_streaming import WhisperStreamingBackend

    lock = threading.Lock()
    in_call = threading.Event()
    concurrent = threading.Event()
    barrier = threading.Barrier(2)

    def fake_mlx(audio, path_or_hf_repo, **kwargs):
        if in_call.is_set():
            concurrent.set()  # two calls overlapped!
        in_call.set()
        time.sleep(0.05)
        in_call.clear()
        return {"text": "", "segments": []}

    backend = WhisperStreamingBackend(mlx_transcribe=fake_mlx, inference_lock=lock)

    def transcribe_one():
        barrier.wait()  # release both threads simultaneously
        # 640 bytes = 320 samples = 20ms @ 16kHz int16
        backend.transcribe(b"\x00" * 640, 16000)

    t1 = threading.Thread(target=transcribe_one)
    t2 = threading.Thread(target=transcribe_one)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not concurrent.is_set(), (
        "two transcribe calls ran concurrently — inference lock did not serialize"
    )


def test_whisper_no_lock_allows_concurrent_transcribe():
    """Belt-and-suspenders: without an inference_lock, two concurrent calls
    DO overlap — proving the test above is meaningful (not trivially green)."""
    from openrecall_server.ingest.whisper_streaming import WhisperStreamingBackend

    in_call = threading.Event()
    concurrent = threading.Event()
    barrier = threading.Barrier(2)

    def fake_mlx(audio, path_or_hf_repo, **kwargs):
        if in_call.is_set():
            concurrent.set()
        in_call.set()
        time.sleep(0.05)
        in_call.clear()
        return {"text": "", "segments": []}

    # No inference_lock — calls should overlap.
    backend = WhisperStreamingBackend(mlx_transcribe=fake_mlx, inference_lock=None)

    def transcribe_one():
        barrier.wait()
        backend.transcribe(b"\x00" * 640, 16000)

    t1 = threading.Thread(target=transcribe_one)
    t2 = threading.Thread(target=transcribe_one)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert concurrent.is_set(), (
        "expected concurrent overlap without lock — test harness is broken"
    )


def test_parakeet_concurrent_transcribe_serialized_by_inference_lock():
    """R4 (Parakeet path): same serialization test for ParakeetStreamingBackend."""
    from openrecall_server.ingest.parakeet_streaming import ParakeetStreamingBackend

    lock = threading.Lock()
    in_call = threading.Event()
    concurrent = threading.Event()
    barrier = threading.Barrier(2)

    class FakeModel:
        def generate(self, mel):
            if in_call.is_set():
                concurrent.set()
            in_call.set()
            time.sleep(0.05)
            in_call.clear()
            return []

    def fake_featurize(pcm, sample_rate):
        return mel  # noqa: F821 — returned below

    mel = object()
    backend = ParakeetStreamingBackend(
        model=FakeModel(), featurize=fake_featurize, inference_lock=lock,
    )

    def transcribe_one():
        barrier.wait()
        backend.transcribe(b"\x00" * 640, 16000)

    t1 = threading.Thread(target=transcribe_one)
    t2 = threading.Thread(target=transcribe_one)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not concurrent.is_set(), (
        "two Parakeet transcribe calls ran concurrently — inference lock did not serialize"
    )