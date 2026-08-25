"""Process-wide load-once shared ASR model cache.

Loads the ASR model once per process (keyed by model name) and returns the
cached instance on every later call, so reconnects don't reload the model.
Holds a shared ``threading.Lock`` for inference serialization (acquired
around ``model.generate`` by the inference worker in a later task).

Thread-safe: a ``threading.Lock`` guards the load so concurrent first
sessions don't double-load. Tests reset the cache via :func:`reset` and
inject a custom ``loader`` to avoid leaking the cached model across cases.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class SharedAsrModel:
    """A shared ASR model + its inference serialization lock.

    ``model`` is the loaded model object (Parakeet) or the model repo-name
    string (Whisper — ``mlx_whisper`` caches the loaded weights at module
    level internally, so the "model" here is just the string identifier).
    ``inference_lock`` is acquired around ``model.generate`` by the
    inference worker (Task 3); it is exposed here so the worker can
    serialize inference without re-architecting the holder.
    """

    def __init__(self, model: Any, model_name: str) -> None:
        self.model = model
        self.model_name = model_name
        self.inference_lock = threading.Lock()


_CACHE: dict[str, SharedAsrModel] = {}
_CACHE_LOCK = threading.Lock()


def get_shared_model(
    model_name: str,
    loader: Callable[[str], Any],
    warmup: Callable[[Any], None] | None = None,
) -> SharedAsrModel:
    """Return the shared model for ``model_name``, loading + warming on first call.

    ``loader(model_name)`` is called once per process per ``model_name`` to
    obtain the model object. ``warmup(model)`` is called once right after
    the first load so the first real hop isn't cold (model compile / weight
    fetch). Warmup failure logs and continues — it must never abort startup.

    Thread-safe: a lock guards the load so concurrent first sessions don't
    double-load. If ``loader`` raises, nothing is cached (the next call
    retries), so a transient import failure doesn't poison the cache.
    """
    entry = _CACHE.get(model_name)
    if entry is not None:
        return entry
    with _CACHE_LOCK:
        # Double-checked locking: another thread may have loaded it
        # while we waited on the lock.
        entry = _CACHE.get(model_name)
        if entry is not None:
            return entry
        logger.info("loading shared ASR model %s (first use)", model_name)
        model = loader(model_name)  # May raise; not cached on failure.
        logger.info("shared ASR model %s ready", model_name)
        entry = SharedAsrModel(model, model_name)
        if warmup is not None:
            try:
                warmup(model)
                logger.info("shared ASR model %s warmup complete", model_name)
            except Exception:
                logger.exception(
                    "shared ASR model %s warmup failed; continuing "
                    "(first real hop will be cold)", model_name,
                )
        _CACHE[model_name] = entry
    return entry


def reset() -> None:
    """Clear the cache. For tests only — never call in production."""
    _CACHE.clear()