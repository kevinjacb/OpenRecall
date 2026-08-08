"""Speaker identification on the live PCM — recognition + clustering + enrollment.

Recognition matches the hop embedding against confirmed/named centroids only:

* >= confirm_threshold  -> confirmed (the ONLY path that adds to the ring buffer)
* [tentative, confirm)   -> tentative, held in a per-candidate assignment buffer;
                             only after N consecutive agreeing tentative hits is
                             it promoted -> confirmed -> folded into the ring
                             buffer. A single tentative match NEVER touches a
                             centroid.
* < tentative_threshold  -> fall through to clustering.

Clustering (the fall-through path) accumulates unmatched embeddings into
in-memory "pending" scratch clusters. A pending cluster becomes a persisted
``Speaker`` row only after ``corroborate_n`` embeddings land within
``corroborate_window_s`` — corroboration-before-mint, so a single stray utterance
never mints an identity. Pending clusters are dropped after ``pending_ttl_s`` of
inactivity.

Cold-start "You" enrollment tags the dominant voice as the wearer, but holds off
when two voices are too close to call (no clear leader), so an interleaved
two-person conversation is never mis-attributed up front.

Speaker ID can fail independently of transcription: on embed failure (or a too-
short window) ``identify`` returns ``None`` and the hop is transcribed with
``speaker=None``; ASR is never blocked.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
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
        # In-memory scratch clusters awaiting corroboration. Each entry:
        #   {speaker_id, embeddings, centroid, first_seen_s, last_seen_s}
        # A pending cluster becomes a speakers row only once it accumulates
        # corroborate_n embeddings within corroborate_window_s.
        self._pending: list[dict] = []
        self._now = now_s or time.monotonic

    def warmup_embedder(self) -> None:
        """Best-effort: warm up the embedder model if it supports warmup.

        run_gateway calls this once at startup so the first real conversation
        pays no model-init cost. No-op for embedders without ``warmup``
        (e.g. FakeSpeakerEmbedder).
        """
        warmup = getattr(self._embedder, "warmup", None)
        if warmup is not None:
            warmup()

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

        if best_id is not None and best_sim >= self._cfg.confirm_threshold:
            self._confirm(best_id, vec, best_sim)
            self._tentative_streak.pop(best_id, None)
            return SpeakerAssignment(best_id, best_sim, "confirmed")

        if best_id is not None and best_sim >= self._cfg.tentative_threshold:
            streak = self._tentative_streak.get(best_id, 0) + 1
            self._tentative_streak[best_id] = streak
            if streak >= self._cfg.corroborate_n:
                self._confirm(best_id, vec, best_sim)
                self._tentative_streak.pop(best_id, None)
                return SpeakerAssignment(best_id, best_sim, "confirmed")
            return SpeakerAssignment(best_id, best_sim, "tentative")

        # below tentative, or cold registry (no centroids) -> cluster.
        if best_id is not None:
            self._tentative_streak.pop(best_id, None)
        return self._cluster(vec)

    def _confirm(self, speaker_id: str, vec: list[float], confidence: float) -> None:
        """The ONLY path that adds to the ring buffer + recomputes the centroid."""
        self._registry.add_confirmed_embedding(speaker_id, vec, confidence)
        self._registry.recompute_centroid(speaker_id)
        self._registry.increment_turn(speaker_id)

    # --- clustering ----------------------------------------------------------

    def _gc_pending(self) -> None:
        """Drop scratch clusters inactive for longer than ``pending_ttl_s``."""
        now = self._now()
        self._pending = [
            p for p in self._pending
            if now - p["last_seen_s"] < self._cfg.pending_ttl_s
        ]

    def _cluster(self, vec: list[float]) -> SpeakerAssignment | None:
        """Accumulate ``vec`` into a pending scratch cluster, minting on
        corroboration. Returns a ``SpeakerAssignment`` only on the minting hop;
        otherwise ``None`` (the hop is transcribed with ``speaker=None`` until
        the cluster corroborates)."""
        self._gc_pending()

        best_idx, best_sim = None, -1.0
        for i, p in enumerate(self._pending):
            sim = _cosine(vec, p["centroid"])
            if sim > best_sim:
                best_sim, best_idx = sim, i

        if best_idx is None or best_sim < self._cfg.cluster_threshold:
            # No close pending cluster -> start a new one. Not corroborated yet.
            self._pending.append({
                "speaker_id": uuid.uuid4().hex,
                "embeddings": [list(vec)],
                "centroid": list(vec),
                "first_seen_s": self._now(),
                "last_seen_s": self._now(),
            })
            return None

        p = self._pending[best_idx]
        p["embeddings"].append(list(vec))
        p["last_seen_s"] = self._now()
        p["centroid"] = _mean_list(p["embeddings"])

        # Corroboration: N embeddings accumulated within the window.
        if (
            len(p["embeddings"]) >= self._cfg.corroborate_n
            and self._now() - p["first_seen_s"] <= self._cfg.corroborate_window_s
        ):
            return self._mint(p)
        return None

    def _mint(self, p: dict) -> SpeakerAssignment:
        """Persist a corroborated scratch cluster as a ``Speaker`` row, fold its
        embeddings into the ring buffer, then run cold-start enrollment."""
        sid = p["speaker_id"]
        model = getattr(self._embedder, "model_name", None)
        self._registry.add_speaker(
            _new_speaker_row(sid, p["centroid"], self._embedder.dim, model)
        )
        for e in p["embeddings"]:
            self._registry.add_confirmed_embedding(sid, e, 1.0)
        self._registry.recompute_centroid(sid)
        # Each corroborating embedding was a turn attributed to this speaker.
        for _ in p["embeddings"]:
            self._registry.increment_turn(sid)
        # Remove the minted scratch cluster.
        self._pending = [q for q in self._pending if q["speaker_id"] != sid]
        self._apply_coldstart_enrollment(sid)
        return SpeakerAssignment(sid, 1.0, "confirmed")

    # --- cold-start "You" enrollment -----------------------------------------

    def _apply_coldstart_enrollment(self, sid: str) -> None:
        """Tag the dominant cold-start voice as the wearer ("You").

        Holds off when evidence is too close to call, so an interleaved
        two-person conversation is never mis-attributed up front:

        * If a wearer is already tagged, never auto-tag again (stable once set).
        * If another pending cluster is approaching corroboration
          (>= corroborate_n - 1 embeddings), hold — wait for more evidence.
        * If this is the sole speaker, tag it.
        * Otherwise tag only a clear leader (turn_count margin >= 1).
        """
        speakers = self._registry.list_speakers()
        if any(s.is_wearer for s in speakers):
            return  # stable: a wearer is already chosen
        for q in self._pending:
            if q["speaker_id"] != sid and len(q["embeddings"]) >= self._cfg.corroborate_n - 1:
                return  # another voice is close to corroborating; hold
        if len(speakers) <= 1:
            self._tag_wearer(sid)
            return
        me = self._registry.get(sid)
        if me is None:
            return
        others = [s for s in speakers if s.speaker_id != sid]
        if not others:
            self._tag_wearer(sid)
            return
        max_other_turns = max(s.turn_count for s in others)
        if me.turn_count - max_other_turns >= 1:
            self._tag_wearer(sid)

    def _tag_wearer(self, sid: str) -> None:
        self._registry.set_is_wearer(sid, True)
        self._registry.set_display_name(sid, "You")
        self._registry.update_enrollment(sid, "implicit")