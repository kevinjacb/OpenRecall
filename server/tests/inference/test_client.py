import contextlib, json, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from openrecall_server.inference.client import (
    HttpSpeakerEmbedder, HttpStreamingBackend, InferenceUnavailable,
)
from openrecall_server.ingest.streaming_transcriber import Token


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *a):  # keep test output clean
        pass

    def _respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append((self.path, None))
        status, payload = self.routes.get(self.path, (404, {}))
        self._respond(status, payload)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        self.seen.append((self.path, json.loads(self.rfile.read(n))))
        status, payload = self.routes.get(self.path, (404, {}))
        self._respond(status, payload)


@pytest.fixture
def server():
    _Handler.routes = {}
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", _Handler
    httpd.shutdown()


def test_transcribe_posts_pcm_and_returns_tokens(server):
    base, h = server
    h.routes["/transcribe"] = (200, {"tokens": [
        {"text": "hi", "start_ms": 0, "end_ms": 100, "sentence_id": 1}]})
    out = HttpStreamingBackend(base).transcribe(b"\x01\x02", 16000)
    assert out == [Token(text="hi", start_ms=0, end_ms=100, sentence_id=1)]
    path, body = h.seen[0]
    assert path == "/transcribe"
    assert body["sample_rate"] == 16000
    # the audio must survive the wire, not merely be present
    from openrecall_server.inference import wire
    assert wire.decode_pcm(body["pcm"]) == b"\x01\x02"


def test_embed_returns_vector(server):
    base, h = server
    h.routes["/embed"] = (200, {"vector": [0.5, -0.25]})
    assert HttpSpeakerEmbedder(base).embed(b"\x01", 16000) == [0.5, -0.25]


def test_embed_returns_none_when_service_says_null(server):
    base, h = server
    h.routes["/embed"] = (200, {"vector": None})
    assert HttpSpeakerEmbedder(base).embed(b"\x01", 16000) is None


def test_dim_comes_from_info(server):
    base, h = server
    h.routes["/info"] = (200, {"embed_dim": 256, "asr_backend": "parakeet", "ready": True})
    assert HttpSpeakerEmbedder(base).dim == 256


def test_dim_is_fetched_once_and_cached(server):
    base, h = server
    h.routes["/info"] = (200, {"embed_dim": 256, "asr_backend": "p", "ready": True})
    e = HttpSpeakerEmbedder(base)
    assert e.dim == 256 and e.dim == 256
    assert sum(1 for p, _ in h.seen if p == "/info") <= 1


def test_unreachable_service_raises_inference_unavailable():
    b = HttpStreamingBackend("http://127.0.0.1:9")  # discard port: nothing listens
    with pytest.raises(InferenceUnavailable):
        b.transcribe(b"\x01", 16000)


def test_server_error_raises_inference_unavailable(server):
    base, h = server
    h.routes["/transcribe"] = (500, {"error": "boom"})
    with pytest.raises(InferenceUnavailable):
        HttpStreamingBackend(base).transcribe(b"\x01", 16000)


class _QuietServer(HTTPServer):
    def handle_error(self, *a):  # the client hangs up mid-sleep; that is the point
        pass


@contextlib.contextmanager
def _serving(handler_cls):
    httpd = _QuietServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()


class _SlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(0.5)  # an order of magnitude past the client's timeout
        body = json.dumps({"tokens": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _GarbageHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = b"not json{{{"  # a 200 whose body is not JSON at all
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_timeout_raises_inference_unavailable():
    """A hung service must degrade like a dead one, not block the worker thread."""
    with _serving(_SlowHandler) as base:
        b = HttpStreamingBackend(base, timeout_s=0.05)
        started = time.monotonic()
        with pytest.raises(InferenceUnavailable):
            b.transcribe(b"\x01", 16000)
        # it gave up on its own deadline rather than riding out the 0.5s sleep
        assert time.monotonic() - started < 0.4


def test_malformed_json_response_raises_inference_unavailable():
    """A 200 carrying garbage is a service failure, not a decode error to leak."""
    with _serving(_GarbageHandler) as base:
        with pytest.raises(InferenceUnavailable):
            HttpStreamingBackend(base).transcribe(b"\x01", 16000)


def test_warmup_probes_the_service(server):
    base, h = server
    h.routes["/info"] = (200, {"embed_dim": 256, "asr_backend": "p", "ready": True})
    HttpSpeakerEmbedder(base).warmup()
    assert [p for p, _ in h.seen] == ["/info"]


def test_warmup_raises_when_service_is_unreachable():
    with pytest.raises(InferenceUnavailable):
        HttpSpeakerEmbedder("http://127.0.0.1:9").warmup()


def test_clients_satisfy_the_protocols(server):
    base, _ = server
    from openrecall_server.ingest.speaker_embedder import SpeakerEmbedder
    from openrecall_server.ingest.streaming_transcriber import StreamingBackend
    assert isinstance(HttpStreamingBackend(base), StreamingBackend)
    assert isinstance(HttpSpeakerEmbedder(base), SpeakerEmbedder)
