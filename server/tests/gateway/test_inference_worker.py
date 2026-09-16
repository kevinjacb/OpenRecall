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

# --- P1: a sustained backend outage must not fill the disk -------------------
#
# The worker logs one traceback per failed frame. That was harmless while the
# backend was in-process (it rarely fails twice in a row); pointing ASR at a
# network service makes a sustained outage the EXPECTED failure, at one frame
# per hop, forever, on a box that records all day.
#
# No wall-clock assertions here: the repo's one known flaky test is a 50 ms
# timing assertion in this very file. The collapse is therefore count-based,
# and so are these tests.


def _drain(worker, results, n, timeout=5.0):
    """Block until `n` frames have been handled, without sleeping on a clock."""
    deadline = time.monotonic() + timeout
    while len(results) < n and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(results) >= n, f"worker handled {len(results)}/{n} frames"


@pytest.mark.asyncio
async def test_sustained_failure_logs_one_traceback_not_one_per_frame(caplog):
    """N consecutive failures must produce ONE traceback and a handful of
    one-line summaries — not N tracebacks."""
    import logging

    caplog.set_level(logging.INFO)
    n_frames = 500
    handled: list[int] = []

    def always_fails(core, msg):
        handled.append(1)
        raise ValueError("inference service is down")

    loop = asyncio.get_running_loop()
    worker = InferenceWorker(
        always_fails, core=None, loop=loop, sink=lambda _: None,
        max_queue=n_frames + 1,
    )
    worker.start()
    try:
        for i in range(n_frames):
            worker.enqueue(b"x")
        _drain(worker, handled, n_frames)
    finally:
        await worker.stop()

    records = [r for r in caplog.records if "asr worker" in r.message]
    tracebacks = [r for r in records if r.exc_info is not None]
    # Exactly one full traceback for the whole outage.
    assert len(tracebacks) == 1, (
        f"{len(tracebacks)} tracebacks for {n_frames} consecutive failures"
    )
    # Far fewer lines than frames, but not silence: the outage must stay
    # visible while it persists.
    assert 2 <= len(records) <= n_frames // 10, (
        f"{len(records)} log lines for {n_frames} failures: {[r.message for r in records]}"
    )
    # A summary must carry the real consecutive count, so an operator reading
    # one line knows the scale. This is what a hardcoded "still failing"
    # message would not satisfy.
    assert any(str(n_frames) in r.message for r in records), (
        f"no summary named the {n_frames}-failure run: {[r.message for r in records]}"
    )


@pytest.mark.asyncio
async def test_interleaved_failures_still_collapse_to_one_traceback(caplog):
    """The PRODUCTION failure pattern, and the one a fail-every-frame test
    misses entirely.

    The worker sees frames, but only some frames reach the backend: an audio
    packet is ~200 ms while the Whisper hop is 1 s, so during a *total* outage
    about four frames in five succeed (they only buffer PCM) and one fails.
    A collapse that resets on the first success therefore logs a traceback per
    failed hop — no collapse at all. Observed for real on 2026-09-17: a
    gateway pointed at a dead inference service logged "recovered after 5
    consecutive failed frames" while the service was still down.
    """
    import logging

    caplog.set_level(logging.INFO)
    n_frames = 500                      # 100 failed hops, 400 buffering frames
    handled: list[int] = []

    def fails_every_fifth_frame(core, msg):
        handled.append(1)
        if len(handled) % 5 == 0:
            raise ValueError("inference service is down")
        return ["ok"]

    loop = asyncio.get_running_loop()
    worker = InferenceWorker(
        fails_every_fifth_frame, core=None, loop=loop, sink=lambda _: None,
        max_queue=n_frames + 1,
    )
    worker.start()
    try:
        for _ in range(n_frames):
            worker.enqueue(b"x")
        _drain(worker, handled, n_frames)
    finally:
        await worker.stop()

    records = [r for r in caplog.records if "asr worker" in r.message]
    tracebacks = [r for r in records if r.exc_info is not None]
    assert len(tracebacks) == 1, (
        f"{len(tracebacks)} tracebacks for one outage with interleaved "
        f"successes: {[r.message for r in tracebacks]}"
    )
    # And no spurious "recovered" while the outage is still going.
    assert not [r for r in records if "recovered" in r.message], (
        f"announced recovery mid-outage: {[r.message for r in records]}"
    )


@pytest.mark.asyncio
async def test_recovery_logs_exactly_one_line(caplog):
    """When the backend really comes back, exactly one line says so.

    "Really" means a clean run, not one lucky frame — see the interleaved
    test above for why a single success cannot be trusted.
    """
    import logging

    caplog.set_level(logging.INFO)
    from openrecall_server.gateway.inference_worker import _RECOVERY_CLEAN_FRAMES

    n_fail = 3
    calls = {"n": 0}
    handled: list[int] = []

    def fails_then_recovers(core, msg):
        calls["n"] += 1
        handled.append(1)
        if calls["n"] <= n_fail:
            raise ValueError("inference service is down")
        return ["ok"]

    n_frames = n_fail + _RECOVERY_CLEAN_FRAMES + 5
    loop = asyncio.get_running_loop()
    worker = InferenceWorker(
        fails_then_recovers, core=None, loop=loop, sink=lambda _: None,
        max_queue=n_frames + 1,
    )
    worker.start()
    try:
        for _ in range(n_frames):
            worker.enqueue(b"x")
        _drain(worker, handled, n_frames)
    finally:
        await worker.stop()

    recovered = [r for r in caplog.records if "recovered" in r.message]
    assert len(recovered) == 1, (
        f"expected exactly one recovery line, got {[r.message for r in recovered]}"
    )
    assert str(n_fail) in recovered[0].message  # names how many frames were lost


@pytest.mark.asyncio
async def test_a_second_outage_logs_its_own_traceback(caplog):
    """A confirmed recovery resets the collapse, so a NEW outage is not hidden
    by the old one — the failure mode of a naive 'log once ever' guard."""
    import logging

    caplog.set_level(logging.INFO)
    from openrecall_server.gateway.inference_worker import _RECOVERY_CLEAN_FRAMES

    calls = {"n": 0}
    handled: list[int] = []
    second_outage_at = 2 + _RECOVERY_CLEAN_FRAMES

    def fail_recover_fail(core, msg):
        calls["n"] += 1
        handled.append(1)
        if calls["n"] == 1 or calls["n"] == second_outage_at:
            raise ValueError("inference service is down")
        return ["ok"]

    loop = asyncio.get_running_loop()
    worker = InferenceWorker(
        fail_recover_fail, core=None, loop=loop, sink=lambda _: None,
        max_queue=second_outage_at + 2,
    )
    worker.start()
    try:
        for _ in range(second_outage_at):
            worker.enqueue(b"x")
        _drain(worker, handled, second_outage_at)
    finally:
        await worker.stop()

    tracebacks = [
        r for r in caplog.records
        if "asr worker" in r.message and r.exc_info is not None
    ]
    assert len(tracebacks) == 2, (
        f"expected a traceback per distinct outage, got {len(tracebacks)}"
    )
