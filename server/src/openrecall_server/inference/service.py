"""HTTP front for ASR + speaker embedding (spec §4).

Exists so inference can run where the GPU is: a CUDA container, or a native
process on macOS where a container cannot reach Metal. The gateway talks to it
through clients implementing the same Protocols, so in-process stays the default.

Handlers run the backends off the event loop: the models are synchronous and
slow, and blocking the loop would stall every other request.

They do NOT use ``asyncio.to_thread``. MLX arrays are bound to the thread that
created them — an MLX model loaded on one thread and invoked on another raises
``RuntimeError: There is no Stream(gpu, 0) in current thread`` (observed with
parakeet-mlx 0.5 / mlx 0.31 while smoke-testing this service). ``to_thread``'s
shared default executor hands successive calls to whichever worker is idle, so
the model would eventually be touched from a thread that did not load it. Each
model therefore gets ONE dedicated thread, for the lifetime of the app, and is
loaded on that same thread at startup — which is exactly what the gateway does
with its single ``asr-worker`` thread, and why the in-process path never hit
this. A single ASR thread also serializes inference, so the shared
``inference_lock`` the gateway threads through its backends is redundant here.

**Nothing may touch a model off its own thread — including a read of
``embedder.dim``**, which is a property that loads the model on the real
backend. Every such access goes through the owning :class:`_ModelRunner`.

One thread per model means a wedged call blocks that model entirely, so each
runner carries a deadline and a bounded backlog: past ``max_inflight`` queued
calls it refuses with 503 rather than growing an unbounded queue that pins a
decoded PCM buffer per entry. ``/info`` reports the backlog, so a wedged worker
is visible to a health check instead of hiding behind a cheerful ``ready``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web

from . import wire

log = logging.getLogger(__name__)

#: Per-model runners, exposed so startup code can load/warm each model ON the
#: thread that will later run it (see the module docstring).
ASR_RUNNER = web.AppKey("asr_runner", object)
EMBED_RUNNER = web.AppKey("embed_runner", object)
#: Warmup outcome + the resolved embedding dimension, read by ``/info``.
READY = web.AppKey("ready", dict)

DEFAULT_REQUEST_TIMEOUT_S = 120.0
DEFAULT_MAX_INFLIGHT = 8

#: 20 s of 16 kHz mono int16 (``utterance_transcriber.DEFAULT_MAX_UTTERANCE_MS``)
#: is 640 000 B of PCM, ~853 KB once base64'd into a JSON body. aiohttp's own
#: default is 1 MiB, which that would quietly sit at 81% of. Pick the number
#: deliberately instead of inheriting it.
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024


class ServiceBusy(Exception):
    """The model is saturated or wedged — a 503, distinct from a model error."""


class _ModelRunner:
    """Owns one model's dedicated thread, its deadline and its backlog.

    ``max_workers=1`` is thread affinity, not a throughput choice: see the
    module docstring. Every call into the model goes through :meth:`run`, so
    there is exactly one place that can violate the affinity invariant.
    """

    def __init__(self, name: str, *, timeout_s: float, max_inflight: int) -> None:
        self._name = name
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._timeout_s = timeout_s
        self._max_inflight = max_inflight
        self._inflight = 0

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def saturated(self) -> bool:
        return self._inflight >= self._max_inflight

    async def run(self, fn, *args):
        """Run ``fn`` on this model's thread, or raise ServiceBusy.

        The in-flight count is released by a done-callback on the executor
        future, **not** by the timeout. A cancelled ``wait_for`` does not stop
        the thread — the work keeps running — so releasing on timeout would let
        a wedged worker accept unbounded new requests that all queue behind it.
        Holding the count until the call truly finishes is what makes the
        backlog bound real.
        """
        if self.saturated:
            raise ServiceBusy(
                f"{self._name} backlog full ({self._inflight} in flight)")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._pool, fn, *args)
        self._inflight += 1
        fut.add_done_callback(self._release)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), self._timeout_s)
        except TimeoutError as exc:
            raise ServiceBusy(
                f"{self._name} did not answer within {self._timeout_s}s") from exc

    def _release(self, _fut) -> None:
        self._inflight -= 1

    def shutdown(self) -> None:
        # wait=False returns immediately, but concurrent.futures registers an
        # atexit hook that joins every worker regardless, so a wedged call still
        # holds the interpreter at exit. Nothing here can prevent that.
        self._pool.shutdown(wait=False)


def _bad_request(message: str) -> web.HTTPBadRequest:
    # json.dumps, not an f-string: the message carries a repr of whatever the
    # client sent, which can contain quotes and backslashes that would make a
    # hand-built body unparseable for the client that has to read the error.
    return web.HTTPBadRequest(
        text=json.dumps({"error": f"invalid request: {message}"}),
        content_type="application/json",
    )


async def _read_audio(request: web.Request) -> tuple[bytes, int]:
    """Parse one audio request body, or raise 400.

    The body parse is inside the same guard as the field reads: a client that
    sends a truncated or non-JSON body is just as malformed as one that omits
    ``pcm``, and both must degrade to 400 rather than an aiohttp 500.
    """
    try:
        body = await request.json()
    except web.HTTPException:
        # An oversized body raises HTTPRequestEntityTooLarge from inside
        # request.json(). Let it through unchanged: rewriting it as "body is
        # not JSON" reports the wrong status and sends the reader hunting a
        # serialization bug instead of a size limit.
        raise
    except Exception as exc:                      # not JSON at all
        raise _bad_request(f"body is not JSON: {exc}") from exc
    try:
        pcm = wire.decode_pcm(body["pcm"])
        sample_rate = int(body["sample_rate"])
    except Exception as exc:                      # missing key, bad base64, bad int
        raise _bad_request(str(exc)) from exc
    return pcm, sample_rate


def build_app(
    *,
    backend,
    embedder,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_inflight: int = DEFAULT_MAX_INFLIGHT,
    client_max_size: int = DEFAULT_MAX_BODY_BYTES,
) -> web.Application:
    app = web.Application(client_max_size=client_max_size)
    asr = _ModelRunner("asr", timeout_s=request_timeout_s, max_inflight=max_inflight)
    embed_runner = _ModelRunner(
        "embed", timeout_s=request_timeout_s, max_inflight=max_inflight)
    app[ASR_RUNNER] = asr
    app[EMBED_RUNNER] = embed_runner
    # embed_dim None means "not resolved yet"; /info resolves it on the embed
    # thread and caches it. asr/embed are set by the startup warmup, if any.
    app[READY] = {"asr": True, "embed": True, "embed_dim": None}

    async def transcribe(request: web.Request) -> web.Response:
        pcm, sample_rate = await _read_audio(request)
        try:
            tokens = await asr.run(backend.transcribe, pcm, sample_rate)
        except ServiceBusy as exc:
            log.warning("transcribe_busy: %s", exc)
            return web.json_response({"error": str(exc)}, status=503)
        except Exception as exc:
            log.exception("transcribe_failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"tokens": [wire.token_to_json(t) for t in tokens]})

    async def embed(request: web.Request) -> web.Response:
        pcm, sample_rate = await _read_audio(request)
        try:
            vector = await embed_runner.run(embedder.embed, pcm, sample_rate)
        except ServiceBusy as exc:
            log.warning("embed_busy: %s", exc)
            return web.json_response({"error": str(exc)}, status=503)
        except Exception as exc:
            log.exception("embed_failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(
            {"vector": None if vector is None else [float(x) for x in vector]})

    async def info(request: web.Request) -> web.Response:
        state = request.app[READY]
        if state["embed_dim"] is None and state["embed"]:
            try:
                # Resolved ON the embed thread: `dim` is a property that loads
                # the model on the real backend.
                state["embed_dim"] = int(
                    await embed_runner.run(lambda: embedder.dim))
            except Exception:
                # Never a silent default. A swallowed failure here becomes
                # embed_dim=0, which HttpSpeakerEmbedder caches for the process
                # lifetime and speaker_identifier._mint then writes into every
                # newly minted Speaker row. This codebase has already lost 269
                # rows to a silent dim default; it does not get a second one.
                log.exception("info_embed_dim_failed")
                state["embed"] = False
        components = {
            "asr": bool(state["asr"]) and not asr.saturated,
            "embed": bool(state["embed"]) and not embed_runner.saturated,
        }
        ok = all(components.values())
        return web.json_response({
            "embed_dim": state["embed_dim"],
            "asr_backend": type(backend).__name__,
            "ready": ok,
            "components": components,
            "inflight": {"asr": asr.inflight, "embed": embed_runner.inflight},
        }, status=200 if ok else 503)

    async def _shutdown(_app: web.Application) -> None:
        asr.shutdown()
        embed_runner.shutdown()

    app.on_cleanup.append(_shutdown)
    app.router.add_post("/transcribe", transcribe)
    app.router.add_post("/embed", embed)
    app.router.add_get("/info", info)
    return app
