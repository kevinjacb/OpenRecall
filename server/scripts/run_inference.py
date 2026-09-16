#!/usr/bin/env python3
"""Run the OpenRecall inference service: ASR + speaker embedding over HTTP.

This is the process that owns the models. It exists so inference can run where
the accelerator is — a CUDA container, or (on macOS) a native process, since
Docker on a Mac cannot reach Metal. The gateway reaches it through the clients
in ``openrecall_server.inference.client``, which implement the same Protocols
as the in-process backends; running everything in one process stays the default.

    pip install -e '.[mlx,speaker]'          # + '.[parakeet]' for the TDT backend
    python scripts/run_inference.py --port 8767

Backend selection reads the *same* ``OPENRECALL_*`` environment the gateway
reads, so one config source drives both processes:

  OPENRECALL_ASR_BACKEND            whisper (default) | parakeet
  OPENRECALL_PARAKEET_MODEL         HuggingFace repo id for the parakeet backend
  OPENRECALL_WHISPER_*              the server-side noise-filter thresholds
  OPENRECALL_SPEAKER_EMBED_MODEL    "fake" for the deterministic test embedder;
                                    anything else (incl. unset) -> Resemblyzer
  OPENRECALL_SPEAKER_MIN_SPEECH_MS  minimum window the embedder will embed

``OPENRECALL_ASR_MODE`` is deliberately *not* read here: scheduling (hop vs
utterance) is the gateway's rolling-buffer concern, above this seam. This
service transcribes exactly the buffer it is handed.

Both models are loaded lazily, ON the dedicated thread that will later run
them (see ``inference/service.py``): MLX arrays belong to the thread that
created them, so loading on the main thread and inferring on a worker raises
"There is no Stream(gpu, 0) in current thread". Loading is triggered by a
dummy inference at startup, so by the time the port accepts traffic the models
are resident and ``/info`` is a true readiness signal.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from aiohttp import web

_WARM_MS = 1000  # 1 s of silence: enough to force a real load + one inference


def build_backend(env, model_override: str | None = None):
    """Construct the ASR backend the env selects, WITHOUT loading its model.

    Mirrors ``gateway.adapter.build_pipeline_factory``'s backend selection —
    same env, same kwargs — with two deliberate differences:

    * no ``ingest.shared_asr_model`` cache. That cache exists so multiple
      gateway *sessions* share one model; this process has exactly one backend,
      so it buys nothing, and going through it would load the model here on the
      main thread, which is precisely what MLX forbids.
    * no ``inference_lock``. The service gives ASR a single dedicated thread,
      so calls are already serialized; the lock would be a no-op.
    """
    from openrecall_server.agent.config import load_agent_config

    cfg = load_agent_config(env)
    if cfg.asr.backend == "parakeet":
        from openrecall_server.ingest.parakeet_streaming import (
            DEFAULT_PARAKEET_MODEL,
            ParakeetStreamingBackend,
        )

        # `--model` is the mlx-whisper repo override and is deliberately not
        # reused here: pointing the Parakeet loader at a Whisper repo would
        # fail confusingly. Parakeet has its own knob (OPENRECALL_PARAKEET_MODEL).
        return ParakeetStreamingBackend(
            model_name=cfg.asr.parakeet_model or DEFAULT_PARAKEET_MODEL)

    from openrecall_server.ingest.whisper_streaming import (
        DEFAULT_MODEL,
        WhisperStreamingBackend,
    )

    w = cfg.whisper
    # Whisper's "model" is the repo-name string, not a loaded object;
    # mlx_whisper caches the weights at module level on first transcribe.
    return WhisperStreamingBackend(
        model=model_override or DEFAULT_MODEL,
        no_speech_threshold=w.no_speech_threshold,
        logprob_threshold=w.logprob_threshold,
        compression_ratio_threshold=w.compression_ratio_threshold,
        condition_on_previous_text=w.condition_on_previous_text,
        hallucination_blocklist_enabled=w.hallucination_blocklist_enabled,
        hallucination_max_words=w.hallucination_max_words,
        hallucination_phrases=w.hallucination_phrases,
    )


def build_embedder(env):
    """Construct the speaker embedder the env selects (model still unloaded).

    Reuses ``gateway.adapter._select_embedder`` rather than re-deriving the
    rule: "fake" means the deterministic test embedder, anything else (including
    unset) means the real Resemblyzer backend. That default was a production
    incident once already; there must be exactly one copy of it.
    """
    from openrecall_server.gateway.adapter import _select_embedder
    from openrecall_server.ingest.speaker_config import load_speaker_config

    return _select_embedder(load_speaker_config(env))


def add_warmup(app, backend, embedder) -> None:
    """Load both models at startup, each on the thread that will run it."""
    from openrecall_server.inference.service import ASR_EXECUTOR, EMBED_EXECUTOR

    silence = b"\x00" * (16000 * 2 * _WARM_MS // 1000)

    def warm_asr():
        # A real inference call: it is what forces the lazy load, and it warms
        # the compiled graph so the first live hop pays nothing.
        backend.transcribe(silence, 16000)

    def warm_embed():
        warmup = getattr(embedder, "warmup", None)   # the fake embedder has none
        if warmup is not None:
            warmup()

    async def _startup(app_):
        loop = asyncio.get_running_loop()
        for name, pool, fn in (("asr", app_[ASR_EXECUTOR], warm_asr),
                               ("embed", app_[EMBED_EXECUTOR], warm_embed)):
            try:
                await loop.run_in_executor(pool, fn)
                logging.info("%s_warm ok", name)
            except Exception:
                # Best-effort: a warm failure must not stop the service from
                # serving — the next real request retries the load.
                logging.exception("%s_warmup_failed", name)
        print(f"models ready: asr={type(backend).__name__} "
              f"embed_dim={getattr(embedder, 'dim', '?')}", flush=True)

    app.on_startup.append(_startup)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default: loopback — this service is unauthenticated)")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--model", default=None, help="override the MLX-whisper model repo")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from openrecall_server.inference.service import build_app

    backend = build_backend(os.environ, model_override=args.model)
    embedder = build_embedder(os.environ)
    app = build_app(backend=backend, embedder=embedder)
    add_warmup(app, backend, embedder)
    print(f"inference service on http://{args.host}:{args.port}", flush=True)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
