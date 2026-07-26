"""Speaker identification on the live PCM — recognition + clustering + enrollment.

Recognition matches the hop embedding against confirmed/named centroids only:

* >= confirm_threshold  -> confirmed (the ONLY path that adds to the ring buffer)
* [tentative, confirm)   -> tentative, held in a per-candidate assignment buffer;
                             only after N consecutive agreeing tentative hits is
                             it promoted -> confirmed -> folded into the ring
                             buffer. A single tentative match NEVER touches a
                             centroid.
* < tentative_threshold  -> fall through to clustering.

Speaker ID can fail independently of transcription: on embed failure (or a too-
short window) ``identify`` returns ``None`` and the hop is transcribed with
``speaker=None``; ASR is never blocked.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .speaker_config import SpeakerConfig
from .speaker_embedder import SpeakerEmbedder
from ..memory.speaker_registry import SpeakerRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeakerAssignment:
    speaker_id: str
    confidence: float
    assignment: str  # "confirmed" | "tentative"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mean_list(rows: list[list[float]]) -> list[float]:
    if not rows:
        return []
    dim = len(rows[0])
    return [sum(r[i] for r in rows) / len(rows) for i in range(dim)]


def _new_speaker_row(speaker_id, centroid, dim, model):
    from datetime import datetime, timezone

    from ..memory.speaker_registry import Speaker

    ts = datetime.now(tz=timezone.utc).isoformat()
    return Speaker(
        speaker_id=speaker_id, display_name=None, is_wearer=False,
        enrollment_status="confirmed", centroid=list(centroid),
        embedding_model=model or "unknown", dim=dim, turn_count=0,
        first_seen=ts, updated_at=ts,
    )


class SpeakerIdentifier:
    def __init__(
        self,
        embedder: SpeakerEmbedder,
        registry: SpeakerRegistry,
        cfg: SpeakerConfig,
        *,
        now_s=None,
    ) -> None:
        self._embedder = embedder
        self._registry = registry
        self._cfg = cfg
        self._tentative_streak: dict[str, int] = {}

    def identify(self, pcm: bytes, sample_rate: int) -> SpeakerAssignment | None:
        try:
            vec = self._embedder.embed(pcm, sample_rate)
        except Exception:
            log.exception("speaker_embed_failed")
            return None
        if vec is None:
            return None  # no speech in hop / too short
        return self._match(vec)

    def _match(self, vec: list[float]) -> SpeakerAssignment | None:
        best_id, best_sim = None, -1.0
        for s in self._registry.matchable():
            sim = _cosine(vec, s.centroid)
            if sim > best_sim:
                best_sim, best_id = sim, s.speaker_id
        if best_id is None:
            return None  # cold registry / no centroids -> clustering (T6)

        if best_sim >= self._cfg.confirm_threshold:
            self._confirm(best_id, vec, best_sim)
            self._tentative_streak.pop(best_id, None)
            return SpeakerAssignment(best_id, best_sim, "confirmed")

        if best_sim >= self._cfg.tentative_threshold:
            streak = self._tentative_streak.get(best_id, 0) + 1
            self._tentative_streak[best_id] = streak
            if streak >= self._cfg.corroborate_n:
                self._confirm(best_id, vec, best_sim)
                self._tentative_streak.pop(best_id, None)
                return SpeakerAssignment(best_id, best_sim, "confirmed")
            return SpeakerAssignment(best_id, best_sim, "tentative")

        # below tentative -> fall through to clustering (T6 fills this in)
        self._tentative_streak.pop(best_id, None)
        return None

    def _confirm(self, speaker_id: str, vec: list[float], confidence: float) -> None:
        """The ONLY path that adds to the ring buffer + recomputes the centroid."""
        self._registry.add_confirmed_embedding(speaker_id, vec, confidence)
        self._registry.recompute_centroid(speaker_id)
        self._registry.increment_turn(speaker_id)