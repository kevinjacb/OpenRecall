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
import math
from typing import Protocol, runtime_checkable

SpeakerVector = list[float]


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


class MlxSpeakerEmbedder:
    """Real Apple-Silicon voice-embedding backend (ECAPA-TDNN / pyannote-style).

    Heavy deps (numpy, the embedding model) are imported lazily inside ``embed``
    so importing this module — and running the unit suite — never loads them.
    Configured via :class:`SpeakerConfig`; the model id/base_url/api_key mirror
    the text embedder. Local-only by default; a cloud base_url is opt-in.

    The local model inference wiring lands with the hardware bring-up; until then
    ``embed`` raises ``NotImplementedError`` so a misconfigured production path
    fails loud rather than silently returning a fake vector.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        dim: int = 0,
        min_speech_ms: int = 500,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self._dim = dim
        self.min_speech_ms = min_speech_ms

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        ms = len(pcm) * 1000 // (sample_rate * 2)
        if ms < self.min_speech_ms:
            return None
        # Lazy import — unit tests never reach here.
        import numpy as np  # noqa: F401  (lazy heavy dep)

        raise NotImplementedError(
            "MlxSpeakerEmbedder.embed requires the local voice-embedding model; "
            "configure SENSE_SPEAKER_EMBED_MODEL and run on Apple Silicon."
        )