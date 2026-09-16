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
"""
from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web

from . import wire

log = logging.getLogger(__name__)

#: The dedicated model threads, exposed so startup code can load/warm each
#: model ON the thread that will later run it (see the module docstring).
ASR_EXECUTOR = web.AppKey("asr_executor", ThreadPoolExecutor)
EMBED_EXECUTOR = web.AppKey("embed_executor", ThreadPoolExecutor)


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
    except Exception as exc:                      # not JSON at all
        raise _bad_request(f"body is not JSON: {exc}") from exc
    try:
        pcm = wire.decode_pcm(body["pcm"])
        sample_rate = int(body["sample_rate"])
    except Exception as exc:                      # missing key, bad base64, bad int
        raise _bad_request(str(exc)) from exc
    return pcm, sample_rate


def build_app(*, backend, embedder) -> web.Application:
    app = web.Application()
    # max_workers=1: thread affinity for the model, not a throughput choice.
    asr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
    embed_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")
    app[ASR_EXECUTOR] = asr_pool
    app[EMBED_EXECUTOR] = embed_pool

    async def transcribe(request: web.Request) -> web.Response:
        pcm, sample_rate = await _read_audio(request)
        loop = asyncio.get_running_loop()
        try:
            tokens = await loop.run_in_executor(
                asr_pool, backend.transcribe, pcm, sample_rate)
        except Exception as exc:
            log.exception("transcribe_failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"tokens": [wire.token_to_json(t) for t in tokens]})

    async def embed(request: web.Request) -> web.Response:
        pcm, sample_rate = await _read_audio(request)
        loop = asyncio.get_running_loop()
        try:
            vector = await loop.run_in_executor(
                embed_pool, embedder.embed, pcm, sample_rate)
        except Exception as exc:
            log.exception("embed_failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(
            {"vector": None if vector is None else [float(x) for x in vector]})

    async def info(_request: web.Request) -> web.Response:
        return web.json_response({
            "embed_dim": int(getattr(embedder, "dim", 0)),
            "asr_backend": type(backend).__name__,
            "ready": True,
        })

    async def _shutdown(_app: web.Application) -> None:
        # wait=False: a request in flight is a model call that cannot be
        # cancelled anyway, and the process is going down.
        asr_pool.shutdown(wait=False)
        embed_pool.shutdown(wait=False)

    app.on_cleanup.append(_shutdown)
    app.router.add_post("/transcribe", transcribe)
    app.router.add_post("/embed", embed)
    app.router.add_get("/info", info)
    return app
