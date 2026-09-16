import asyncio
import json
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.inference import wire
from openrecall_server.inference.service import build_app
from openrecall_server.ingest.streaming_transcriber import Token


class FakeBackend:
    def __init__(self):
        self.calls = []

    def transcribe(self, pcm, sample_rate):
        self.calls.append((pcm, sample_rate))
        return [Token(text="ok", start_ms=0, end_ms=10, sentence_id=2)]


class FakeEmbedder:
    dim = 7

    def __init__(self, vector=None):
        self.vector, self.calls = vector, []

    def embed(self, pcm, sample_rate):
        self.calls.append((pcm, sample_rate))
        return self.vector

    def warmup(self):
        pass


@pytest.fixture
async def client():
    be, em = FakeBackend(), FakeEmbedder(vector=[1.0, 2.0])
    app = build_app(backend=be, embedder=em)
    c = TestClient(TestServer(app))
    await c.start_server()
    yield c, be, em
    await c.close()


async def test_transcribe_decodes_pcm_and_returns_tokens(client):
    c, be, _ = client
    r = await c.post("/transcribe", json={"pcm": wire.encode_pcm(b"\x01\x02"),
                                          "sample_rate": 16000})
    assert r.status == 200
    assert (await r.json())["tokens"] == [
        {"text": "ok", "start_ms": 0, "end_ms": 10, "sentence_id": 2}]
    assert be.calls == [(b"\x01\x02", 16000)]   # the bytes reached the backend intact


async def test_embed_returns_vector(client):
    c, _, em = client
    r = await c.post("/embed", json={"pcm": wire.encode_pcm(b"\x01"), "sample_rate": 16000})
    assert r.status == 200
    assert (await r.json())["vector"] == [1.0, 2.0]
    assert em.calls == [(b"\x01", 16000)]   # the bytes reached the embedder intact


async def test_embed_null_vector_is_json_null():
    app = build_app(backend=FakeBackend(), embedder=FakeEmbedder(vector=None))
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        r = await c.post("/embed", json={"pcm": wire.encode_pcm(b"\x01"), "sample_rate": 16000})
        assert r.status == 200
        assert (await r.json())["vector"] is None
    finally:
        await c.close()


async def test_info_reports_embed_dim(client):
    c, _, _ = client
    assert (await (await c.get("/info")).json())["embed_dim"] == 7


async def test_malformed_body_is_400_not_500(client):
    c, _, _ = client
    for bad in ({"sample_rate": 16000}, {"pcm": "!!not base64!!", "sample_rate": 16000},
                {"pcm": wire.encode_pcm(b"\x01")}):
        r = await c.post("/transcribe", json=bad)
        assert r.status == 400, f"expected 400 for {bad}, got {r.status}"


async def test_body_that_is_not_json_at_all_is_400_not_500(client):
    """request.json() raises before any field validation runs; a client that
    sends garbage must get 400, not an aiohttp 500 traceback."""
    c, _, _ = client
    for path in ("/transcribe", "/embed"):
        r = await c.post(path, data=b"not json{{{",
                         headers={"Content-Type": "application/json"})
        assert r.status == 400, f"expected 400 for garbage body on {path}, got {r.status}"


async def test_malformed_embed_body_is_400_not_500(client):
    c, _, em = client
    r = await c.post("/embed", json={"sample_rate": 16000})
    assert r.status == 400
    assert em.calls == []   # never reached the model


async def test_backend_runs_off_the_event_loop_thread(client):
    """The models are synchronous and slow; running one on the loop stalls
    every other request. Asserted by thread identity, not by wall clock —
    the repo already has one timing-based version of this test and it is the
    only known flaky test in the suite."""
    c, be, _ = client
    seen = {}

    def record(pcm, sample_rate):
        seen["thread"] = threading.get_ident()
        return []

    be.transcribe = record
    r = await c.post("/transcribe", json={"pcm": wire.encode_pcm(b"\x01"),
                                          "sample_rate": 16000})
    assert r.status == 200
    assert seen, "backend was never called"
    assert seen["thread"] != threading.get_ident(), (
        "backend ran on the event loop thread")


async def test_embedder_runs_off_the_event_loop_thread(client):
    c, _, em = client
    seen = {}

    def record(pcm, sample_rate):     # implicitly returns None: "no vector"
        seen["thread"] = threading.get_ident()

    em.embed = record
    r = await c.post("/embed", json={"pcm": wire.encode_pcm(b"\x01"),
                                     "sample_rate": 16000})
    assert r.status == 200
    assert seen, "embedder was never called"
    assert seen["thread"] != threading.get_ident(), (
        "embedder ran on the event loop thread")


async def test_every_asr_call_runs_on_the_same_thread(client):
    """MLX arrays belong to the thread that created them: a model loaded on
    one thread and invoked on another raises "There is no Stream(gpu, 0) in
    current thread" (parakeet-mlx 0.5 / mlx 0.31, hit in the Step 6 smoke).
    So the ASR backend must own ONE thread, not a share of a general pool.

    Concurrent requests are what discriminates: a shared default executor
    (asyncio.to_thread) hands three overlapping calls to three different
    workers; a max_workers=1 pool serializes them onto one.
    """
    import time
    c, be, _ = client
    threads = []

    def record(pcm, sample_rate):
        threads.append(threading.get_ident())
        time.sleep(0.05)   # long enough that the three calls overlap
        return []

    be.transcribe = record
    body = {"pcm": wire.encode_pcm(b"\x01"), "sample_rate": 16000}
    rs = await asyncio.gather(*(c.post("/transcribe", json=body) for _ in range(3)))
    assert [r.status for r in rs] == [200, 200, 200]
    assert len(threads) == 3
    assert len(set(threads)) == 1, f"ASR ran on {len(set(threads))} threads: {threads}"
    assert threads[0] != threading.get_ident()


async def test_backend_failure_is_500_with_a_message(client):
    c, be, _ = client

    def boom(pcm, sample_rate):
        raise RuntimeError("model exploded")

    be.transcribe = boom
    r = await c.post("/transcribe", json={"pcm": wire.encode_pcm(b"\x01"),
                                          "sample_rate": 16000})
    assert r.status == 500
    assert "model exploded" in (await r.json())["error"]


async def test_embedder_failure_is_500_with_a_message(client):
    c, _, em = client

    def boom(pcm, sample_rate):
        raise RuntimeError("encoder exploded")

    em.embed = boom
    r = await c.post("/embed", json={"pcm": wire.encode_pcm(b"\x01"),
                                     "sample_rate": 16000})
    assert r.status == 500
    assert "encoder exploded" in (await r.json())["error"]


async def test_error_bodies_are_valid_json_even_when_the_message_has_quotes(client):
    """The 400 body is assembled by hand; a message carrying a double quote
    must not produce a body the client's json.loads() chokes on."""
    c, _, _ = client
    r = await c.post("/transcribe",
                     json={"pcm": wire.encode_pcm(b"\x01"), "sample_rate": 'he said "hi"'})
    assert r.status == 400
    body = await r.json()          # raises if the hand-built body is not JSON
    assert "error" in body


async def test_clients_can_talk_to_the_service(client):
    """End-to-end over a real socket: the Task 1 clients against this service."""
    from openrecall_server.inference.client import HttpSpeakerEmbedder, HttpStreamingBackend

    c, be, _ = client
    base = str(c.make_url("/")).rstrip("/")
    import asyncio

    tokens = await asyncio.to_thread(HttpStreamingBackend(base).transcribe, b"\x01\x02", 16000)
    assert tokens == [Token(text="ok", start_ms=0, end_ms=10, sentence_id=2)]
    assert be.calls == [(b"\x01\x02", 16000)]
    assert await asyncio.to_thread(lambda: HttpSpeakerEmbedder(base).dim) == 7


async def test_every_embed_call_runs_on_the_same_thread(client):
    """The mirror of the ASR affinity test. The service docstring claims a
    dedicated thread for *both* models, but only ASR's half was enforced —
    so the embed half could regress silently the day the embedder is swapped
    for an accelerator-backed one (speaker_embedder.py contemplates exactly
    that: "migration to ECAPA-TDNN / pyannote without architectural changes").
    """
    import time
    c, _, em = client
    threads = []

    def record(pcm, sample_rate):
        threads.append(threading.get_ident())
        time.sleep(0.05)   # long enough that the three calls overlap
        return None

    em.embed = record
    body = {"pcm": wire.encode_pcm(b"\x01"), "sample_rate": 16000}
    rs = await asyncio.gather(*(c.post("/embed", json=body) for _ in range(3)))
    assert [r.status for r in rs] == [200, 200, 200]
    assert len(threads) == 3
    assert len(set(threads)) == 1, f"embed ran on {len(set(threads))} threads: {threads}"
    assert threads[0] != threading.get_ident()


async def test_dim_is_read_on_the_embed_thread_not_the_loop():
    """`dim` is a property that loads the model on the real backend, so reading
    it on the event loop is the same affinity violation as running inference
    there — and it is the one the review found in the startup banner."""
    class ThreadRecordingEmbedder:
        def __init__(self):
            self.dim_threads = []
        def embed(self, pcm, sample_rate):
            return None
        @property
        def dim(self):
            self.dim_threads.append(threading.get_ident())
            return 256

    em = ThreadRecordingEmbedder()
    c = TestClient(TestServer(build_app(backend=FakeBackend(), embedder=em)))
    await c.start_server()
    try:
        r = await c.get("/info")
        assert (await r.json())["embed_dim"] == 256
        assert em.dim_threads, "dim was never read"
        assert em.dim_threads[0] != threading.get_ident(), (
            "dim was read on the event loop thread")
    finally:
        await c.close()


async def test_a_failing_dim_never_becomes_a_silent_zero():
    """The worst failure this service can have.

    `getattr(embedder, "dim", 0)` swallows AttributeError — which a torch or
    numpy version skew inside the loader raises routinely — and substitutes 0.
    HttpSpeakerEmbedder caches that 0 for the process lifetime, and
    speaker_identifier._mint writes it into every newly minted Speaker row.
    This codebase has already lost 269 rows to a silent dim default
    (dim=16, 2026-08-25); a 0 would be the same incident with a new cause.
    """
    class BrokenDimEmbedder:
        def embed(self, pcm, sample_rate):
            return None
        @property
        def dim(self):
            raise AttributeError("'VoiceEncoder' object has no attribute 'foo'")

    c = TestClient(TestServer(
        build_app(backend=FakeBackend(), embedder=BrokenDimEmbedder())))
    await c.start_server()
    try:
        r = await c.get("/info")
        body = await r.json()
        assert body["embed_dim"] != 0, "a broken dim was reported as 0"
        assert body["embed_dim"] is None
        assert body["ready"] is False
        assert body["components"]["embed"] is False
        assert r.status == 503, "an unusable embedder must not report 200/ready"
    finally:
        await c.close()


async def test_a_wedged_model_returns_503_rather_than_hanging_forever():
    """One thread per model means a wedged call blocks that model completely.
    Without a server-side deadline the caller just hangs: the only timeout is
    in the client, so the service itself would never say anything."""
    import threading as _t
    release = _t.Event()

    class WedgingBackend:
        def transcribe(self, pcm, sample_rate):
            release.wait(30)       # never set during the assertion window
            return []

    app = build_app(backend=WedgingBackend(), embedder=FakeEmbedder(),
                    request_timeout_s=0.2)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        r = await c.post("/transcribe", json={"pcm": wire.encode_pcm(b"\x01"),
                                              "sample_rate": 16000})
        assert r.status == 503, "a wedged model must be distinguishable from a crash"
        assert "within" in (await r.json())["error"]
    finally:
        release.set()
        await c.close()


async def test_a_wedged_model_bounds_its_backlog_instead_of_queueing_forever():
    """ThreadPoolExecutor's queue is unbounded and each entry pins its decoded
    PCM, so a gateway retrying against a wedged worker grows it without limit.
    Past max_inflight the service must refuse fast rather than accumulate.

    The in-flight count must be released by the *work* finishing, not by the
    timeout firing — cancelling a wait_for does not stop the thread, so
    releasing on timeout would let an unbounded number of requests queue up
    behind a wedge while the counter read zero.
    """
    import threading as _t
    release = _t.Event()
    started = []

    class WedgingBackend:
        def transcribe(self, pcm, sample_rate):
            started.append(1)
            release.wait(30)
            return []

    app = build_app(backend=WedgingBackend(), embedder=FakeEmbedder(),
                    request_timeout_s=0.2, max_inflight=2)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        body = {"pcm": wire.encode_pcm(b"\x01"), "sample_rate": 16000}
        # Two fill the backlog (one running, one queued); both time out at 503.
        first = await asyncio.gather(*(c.post("/transcribe", json=body)
                                       for _ in range(2)))
        assert [r.status for r in first] == [503, 503]
        # The wedge still holds both slots, so the next one is refused
        # immediately as backlog-full rather than queued behind it.
        r = await c.post("/transcribe", json=body)
        assert r.status == 503
        assert "backlog full" in (await r.json())["error"], (
            "the third request was queued rather than refused — the in-flight "
            "count was released by the timeout instead of by the work")
        info = await (await c.get("/info")).json()
        assert info["ready"] is False and info["components"]["asr"] is False
    finally:
        release.set()
        await c.close()


async def test_an_oversized_body_is_413_not_a_confusing_400(client):
    """aiohttp raises HTTPRequestEntityTooLarge from inside request.json().
    Rewriting it as "body is not JSON" reports the wrong status and sends the
    reader hunting a serialization bug instead of a size limit."""
    app = build_app(backend=FakeBackend(), embedder=FakeEmbedder(),
                    client_max_size=1024)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        r = await c.post("/transcribe", json={"pcm": wire.encode_pcm(b"\x00" * 4096),
                                              "sample_rate": 16000})
        assert r.status == 413, f"expected 413 for an oversized body, got {r.status}"
    finally:
        await c.close()


async def test_the_default_body_limit_clears_the_longest_designed_utterance():
    """utterance_transcriber.DEFAULT_MAX_UTTERANCE_MS is 20 s, which is
    640 000 B of 16 kHz mono int16 and ~853 KB base64'd into a JSON body.
    aiohttp's own 1 MiB default would have sat at 81% of a limit nobody chose.
    """
    from openrecall_server.ingest.utterance_transcriber import (
        DEFAULT_MAX_UTTERANCE_MS,
    )
    from openrecall_server.inference.service import DEFAULT_MAX_BODY_BYTES

    worst_case_pcm = 16000 * 2 * DEFAULT_MAX_UTTERANCE_MS // 1000
    body = len(json.dumps({"pcm": wire.encode_pcm(b"\x00" * worst_case_pcm),
                           "sample_rate": 16000}))
    assert body < DEFAULT_MAX_BODY_BYTES, (
        f"the longest designed utterance is {body} B, over the "
        f"{DEFAULT_MAX_BODY_BYTES} B limit")
