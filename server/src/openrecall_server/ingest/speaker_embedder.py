"""Speaker-embedding model seam — provider-agnostic, local-first.

Mirrors memory/embeddings.py: one Protocol, a fake for unit tests, and a real
mlx/ONNX impl that lazy-imports its heavy deps inside embed() so importing this
module never requires numpy/mlx. Vectors are ``list[float]`` (not numpy) so the
whole unit suite runs without the ``mlx`` extra.

``embed`` returns ``None`` when the window is too short / too quiet for a usable
vector — the identifier treats ``None`` as "no speech in hop" (never embeds,
never mints, never blocks transcription).
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

SpeakerVector = list[float]


def _pcm_to_float32(pcm: bytes, sample_rate: int):
    """int16-LE PCM bytes -> float32 numpy array in [-1, 1].

    Returns ``None`` when ``sample_rate != 16000`` (the caller logs). Pure;
    imports numpy lazily so the unit suite runs without it. Resemblyzer expects
    16 kHz mono float32 in [-1, 1].
    """
    if sample_rate != 16000:
        return None
    import numpy as np

    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


@runtime_checkable
class SpeakerEmbedder(Protocol):
    dim: int

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        """Embed one PCM window, or None when too short/quiet."""
        ...


class FakeSpeakerEmbedder:
    """Deterministic, dependency-free embedder for unit tests.

    Derives a fixed-dim unit vector from a hash of the PCM so identical audio
    yields identical vectors and different audio yields different vectors.
    Returns None when the window is shorter than ``min_speech_ms``.
    """

    def __init__(self, dim: int = 16, min_speech_ms: int = 500) -> None:
        self.dim = dim
        self.min_speech_ms = min_speech_ms

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        ms = len(pcm) * 1000 // (sample_rate * 2)
        if ms < self.min_speech_ms:
            return None
        digest = hashlib.sha256(pcm).digest()
        raw = (digest * ((self.dim // len(digest)) + 1))[: self.dim]
        vec = [(b - 128) / 128.0 for b in raw]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class ResemblyzerSpeakerEmbedder:
    """Real local speaker-embedding backend (Resemblyzer GE2E).

    Chosen for v1 due to its mature API, lightweight CPU inference, and
    permissive licensing. The :class:`SpeakerEmbedder` Protocol allows
    migration to ECAPA-TDNN / pyannote without architectural changes;
    Resemblyzer is not the state of the art but is the right v1 backend.

    Heavy deps (``resemblyzer``, ``numpy``) are imported lazily inside
    ``_ensure_ready`` so importing this module — and constructing this class —
    never loads them. ``dim`` is discovered from a dummy inference (never
    hardcoded), so a future backend with a different dim is accommodated with
    no change to the identifier (the identifier is already dim-agnostic via
    ``_cosine``'s length guard).

    Local-only by default; the remote ``base_url``/``api_key`` path is not
    implemented in v1 (speaker embeddings take audio, not text, so an
    OpenAI-compatible text ``/embeddings`` endpoint does not apply).
    """

    _SUPPORTED_RATE = 16000
    _WARMUP_MS = 1600  # Resemblyzer wants >= ~1.6 s for a stable embedding

    def __init__(
        self, *, min_speech_ms: int = 500, model_name: str = "resemblyzer"
    ) -> None:
        # Cheap: stores config only. Does NOT load the model — so adapter
        # selection + unit tests can construct this class without
        # resemblyzer/numpy installed.
        self.model_name = model_name
        self.min_speech_ms = min_speech_ms
        self._encoder = None
        self._dim: int | None = None
        self._lock = threading.Lock()

    @property
    def dim(self) -> int:
        # Discovered from the model, not assumed. Triggers _ensure_ready on
        # first access (the identifier reads dim at mint time, after the first
        # embed() has already loaded the encoder).
        self._ensure_ready()
        assert self._dim is not None
        return self._dim

    def warmup(self) -> None:
        """Load the model + run a dummy inference so the first real hop pays
        nothing. Best-effort; ``run_gateway`` calls this at startup."""
        self._ensure_ready()

    def _ensure_ready(self) -> None:
        # Thread-safe singleton init (double-checked locking). The gateway
        # runs embed() via asyncio.to_thread; concurrent sessions could race
        # the first-call init.
        if self._encoder is not None:
            return
        with self._lock:
            if self._encoder is not None:
                return
            from resemblyzer import VoiceEncoder
            import numpy as np

            log.info("speaker_embedder_loading model=%s", self.model_name)
            self._encoder = VoiceEncoder()
            # One dummy inference: warms the graph AND discovers dim via
            # shape[0]. Low-amplitude noise; the value is irrelevant, only
            # the dimension matters.
            n = self._SUPPORTED_RATE * self._WARMUP_MS // 1000
            dummy = (np.random.randn(n).astype(np.float32)) * 1e-3
            vec = self._encoder.embed_utterance(dummy)
            self._dim = int(vec.shape[0])
            log.info("speaker_embedder_ready dim=%d", self._dim)

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        if sample_rate != self._SUPPORTED_RATE:
            log.warning(
                "unsupported sample rate %d (speaker disabled for hop)",
                sample_rate,
            )
            return None
        ms = len(pcm) * 1000 // (sample_rate * 2)
        if ms < max(self.min_speech_ms, self._WARMUP_MS):
            return None  # too short for a stable embedding
        wav = _pcm_to_float32(pcm, sample_rate)
        if wav is None:
            return None
        self._ensure_ready()
        vec = self._encoder.embed_utterance(wav)
        # One ndarray -> list[float] conversion at the boundary. Plain floats
        # so JSON storage round-trips cleanly.
        return [float(x) for x in vec.tolist()]