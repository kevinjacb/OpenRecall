"""Persisted speaker registry — voiceprint->named-person identity store.

Two tables: ``speakers`` (one row per corroborated/confirmed speaker; the
centroid is an EMA blob) and ``speaker_embeddings`` (a ring buffer of the last N
confirmed embeddings per speaker, trimmed of outliers before the centroid is
recomputed). No raw audio is stored — only embeddings.

Mirrors EventStore/AtomStore: one Protocol + InMemory + Sqlite, both thread-safe
(``check_same_thread=False`` + ``Lock``). Pending (uncorroborated) scratch
clusters live in the :class:`SpeakerIdentifier`'s memory, not here — they
become a ``speakers`` row only after corroboration.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..ingest.speaker_config import SpeakerConfig


class Speaker(BaseModel):
    """A frozen snapshot of one registry row (read view)."""

    model_config = ConfigDict(frozen=True)

    speaker_id: str
    display_name: str | None
    is_wearer: bool
    enrollment_status: str  # "implicit" | "confirmed"
    centroid: list[float] | None
    embedding_model: str
    dim: int
    turn_count: int
    first_seen: str
    updated_at: str


@runtime_checkable
class SpeakerRegistry(Protocol):
    def get(self, speaker_id: str) -> Speaker | None: ...
    def matchable(self) -> list[Speaker]: ...
    def add_speaker(self, speaker: Speaker) -> None: ...
    def add_confirmed_embedding(self, speaker_id: str, embedding: list[float],
                                confidence: float) -> None: ...
    def ring_buffer(self, speaker_id: str) -> list[list[float]]: ...
    def recompute_centroid(self, speaker_id: str) -> None: ...
    def centroid(self, speaker_id: str) -> list[float] | None: ...
    def increment_turn(self, speaker_id: str) -> int: ...
    def set_display_name(self, speaker_id: str, name: str) -> None: ...
    def update_enrollment(self, speaker_id: str, status: str) -> None: ...
    def list_speakers(self) -> list[Speaker]: ...
    def delete_speaker(self, speaker_id: str) -> None: ...


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _blob(v: list[float]) -> bytes:
    return json.dumps(v).encode("utf-8")


def _unblob(b: bytes | None) -> list[float] | None:
    if b is None:
        return None
    return list(json.loads(b.decode("utf-8")))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mean(rows: list[list[float]]) -> list[float]:
    if not rows:
        return []
    dim = len(rows[0])
    return [sum(r[i] for r in rows) / len(rows) for i in range(dim)]


class _CentroidOps:
    """Shared ring-buffer + outlier-trim + EMA logic for both backends."""

    def __init__(self, cfg: SpeakerConfig) -> None:
        self.cfg = cfg

    def trim(self, rows: list[list[float]]) -> list[list[float]]:
        """Drop the farthest ``outlier_trim_pct`` fraction from the buffer.

        Distance is measured from the buffer's own mean. Trims at most
        ``floor(len * pct)`` rows; a buffer of <= 2 is returned untrimmed.
        """
        if len(rows) <= 2:
            return list(rows)
        k = int(len(rows) * self.cfg.outlier_trim_pct)
        if k <= 0:
            return list(rows)
        m = _mean(rows)
        # lowest cosine from the mean == farthest
        ranked = sorted(rows, key=lambda r: _cosine(r, m))
        return ranked[k:]  # drop the k farthest

    def ema_centroid(self, old: list[float] | None, target: list[float]) -> list[float]:
        """centroid = target if old is None else alpha*target + (1-alpha)*old."""
        if old is None:
            return list(target)
        a = self.cfg.ema_alpha
        return [a * t + (1 - a) * o for t, o in zip(target, old)]


class InMemorySpeakerRegistry:
    def __init__(self, cfg: SpeakerConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._speakers: dict[str, dict] = {}
        self._bufs: dict[str, list[tuple[list[float], float]]] = {}

    def add_speaker(self, speaker: Speaker) -> None:
        with self._lock:
            self._speakers[speaker.speaker_id] = {
                "speaker_id": speaker.speaker_id,
                "display_name": speaker.display_name,
                "is_wearer": speaker.is_wearer,
                "enrollment_status": speaker.enrollment_status,
                "centroid": speaker.centroid,
                "embedding_model": speaker.embedding_model,
                "dim": speaker.dim,
                "turn_count": speaker.turn_count,
                "first_seen": speaker.first_seen,
                "updated_at": speaker.updated_at,
            }
            self._bufs.setdefault(speaker.speaker_id, [])

    def _snapshot(self, d: dict) -> Speaker:
        return Speaker(
            speaker_id=d["speaker_id"], display_name=d["display_name"],
            is_wearer=d["is_wearer"], enrollment_status=d["enrollment_status"],
            centroid=d["centroid"], embedding_model=d["embedding_model"],
            dim=d["dim"], turn_count=d["turn_count"],
            first_seen=d["first_seen"], updated_at=d["updated_at"],
        )

    def get(self, speaker_id: str) -> Speaker | None:
        with self._lock:
            d = self._speakers.get(speaker_id)
            return self._snapshot(d) if d else None

    def matchable(self) -> list[Speaker]:
        with self._lock:
            return [self._snapshot(d) for d in self._speakers.values()
                    if d["centroid"] is not None]

    def list_speakers(self) -> list[Speaker]:
        with self._lock:
            return [self._snapshot(d) for d in self._speakers.values()]

    def add_confirmed_embedding(self, speaker_id, embedding, confidence):
        with self._lock:
            buf = self._bufs.setdefault(speaker_id, [])
            buf.append((list(embedding), confidence))
            if len(buf) > self.cfg.ring_buffer_n:
                del buf[: len(buf) - self.cfg.ring_buffer_n]

    def ring_buffer(self, speaker_id):
        with self._lock:
            return [list(e) for e, _ in self._bufs.get(speaker_id, [])]

    def centroid(self, speaker_id):
        with self._lock:
            d = self._speakers.get(speaker_id)
            return list(d["centroid"]) if d and d["centroid"] else None

    def recompute_centroid(self, speaker_id):
        with self._lock:
            buf = [e for e, _ in self._bufs.get(speaker_id, [])]
            d = self._speakers.get(speaker_id)
            if not buf or d is None:
                return
            ops = _CentroidOps(self.cfg)
            trimmed = ops.trim(buf)
            target = _mean(trimmed)
            d["centroid"] = ops.ema_centroid(d["centroid"], target)
            d["updated_at"] = _now_iso()

    def increment_turn(self, speaker_id):
        with self._lock:
            d = self._speakers.get(speaker_id)
            if d is None:
                raise KeyError(speaker_id)
            d["turn_count"] += 1
            d["updated_at"] = _now_iso()
            return d["turn_count"]

    def set_display_name(self, speaker_id, name):
        with self._lock:
            d = self._speakers.get(speaker_id)
            if d is None:
                raise KeyError(speaker_id)
            d["display_name"] = name
            d["updated_at"] = _now_iso()

    def update_enrollment(self, speaker_id, status):
        with self._lock:
            d = self._speakers.get(speaker_id)
            if d is None:
                raise KeyError(speaker_id)
            d["enrollment_status"] = status
            d["updated_at"] = _now_iso()

    def delete_speaker(self, speaker_id):
        with self._lock:
            self._speakers.pop(speaker_id, None)
            self._bufs.pop(speaker_id, None)


class SqliteSpeakerRegistry:
    def __init__(self, path, cfg: SpeakerConfig) -> None:
        self.cfg = cfg
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS speakers (
                speaker_id         TEXT PRIMARY KEY,
                display_name        TEXT,
                is_wearer           INTEGER NOT NULL,
                enrollment_status   TEXT NOT NULL,
                centroid_embedding   BLOB,
                embedding_model      TEXT NOT NULL,
                dim                  INTEGER NOT NULL,
                turn_count           INTEGER NOT NULL DEFAULT 0,
                first_seen           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS speaker_embeddings (
                speaker_id  TEXT NOT NULL,
                embedding   BLOB NOT NULL,
                confidence  REAL NOT NULL,
                created_at  TEXT NOT NULL,
                FOREIGN KEY (speaker_id) REFERENCES speakers(speaker_id)
            );
            """
        )
        self._conn.commit()

    def _row(self, r) -> Speaker:
        centroid = _unblob(r[4])
        return Speaker(
            speaker_id=r[0], display_name=r[1], is_wearer=bool(r[2]),
            enrollment_status=r[3], centroid=centroid, embedding_model=r[5],
            dim=r[6], turn_count=r[7], first_seen=r[8], updated_at=r[9],
        )

    def add_speaker(self, speaker: Speaker) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO speakers "
                "(speaker_id, display_name, is_wearer, enrollment_status, "
                " centroid_embedding, embedding_model, dim, turn_count, first_seen, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (speaker.speaker_id, speaker.display_name, int(speaker.is_wearer),
                 speaker.enrollment_status,
                 _blob(speaker.centroid) if speaker.centroid else None,
                 speaker.embedding_model, speaker.dim, speaker.turn_count,
                 speaker.first_seen, speaker.updated_at),
            )
            self._conn.commit()

    def get(self, speaker_id):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM speakers WHERE speaker_id=?", (speaker_id,)
            ).fetchone()
        return self._row(r) if r else None

    def matchable(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM speakers WHERE centroid_embedding IS NOT NULL"
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_speakers(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM speakers").fetchall()
        return [self._row(r) for r in rows]

    def add_confirmed_embedding(self, speaker_id, embedding, confidence):
        with self._lock:
            self._conn.execute(
                "INSERT INTO speaker_embeddings (speaker_id, embedding, confidence, created_at) "
                "VALUES (?, ?, ?, ?)",
                (speaker_id, _blob(list(embedding)), confidence, _now_iso()),
            )
            # cap to ring_buffer_n: delete oldest beyond N
            n = self.cfg.ring_buffer_n
            self._conn.execute(
                "DELETE FROM speaker_embeddings WHERE rowid IN ("
                "  SELECT rowid FROM speaker_embeddings WHERE speaker_id=? "
                "  ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (speaker_id, n),
            )
            self._conn.commit()

    def ring_buffer(self, speaker_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT embedding FROM speaker_embeddings WHERE speaker_id=? "
                "ORDER BY created_at ASC", (speaker_id,),
            ).fetchall()
        return [_unblob(r[0]) for r in rows if r[0] is not None]

    def centroid(self, speaker_id):
        with self._lock:
            r = self._conn.execute(
                "SELECT centroid_embedding FROM speakers WHERE speaker_id=?",
                (speaker_id,),
            ).fetchone()
        return _unblob(r[0]) if r and r[0] else None

    def recompute_centroid(self, speaker_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT embedding FROM speaker_embeddings WHERE speaker_id=? "
                "ORDER BY created_at ASC", (speaker_id,),
            ).fetchall()
            buf = [_unblob(r[0]) for r in rows if r[0] is not None]
            r = self._conn.execute(
                "SELECT centroid_embedding FROM speakers WHERE speaker_id=?",
                (speaker_id,),
            ).fetchone()
            if not buf or r is None:
                return
            old = _unblob(r[0]) if r[0] else None
            ops = _CentroidOps(self.cfg)
            trimmed = ops.trim(buf)
            target = _mean(trimmed)
            self._conn.execute(
                "UPDATE speakers SET centroid_embedding=?, updated_at=? WHERE speaker_id=?",
                (_blob(ops.ema_centroid(old, target)), _now_iso(), speaker_id),
            )
            self._conn.commit()

    def increment_turn(self, speaker_id):
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET turn_count=turn_count+1, updated_at=? "
                "WHERE speaker_id=?", (_now_iso(), speaker_id),
            )
            self._conn.commit()
            r = self._conn.execute(
                "SELECT turn_count FROM speakers WHERE speaker_id=?", (speaker_id,),
            ).fetchone()
            return int(r[0]) if r else 0

    def set_display_name(self, speaker_id, name):
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET display_name=?, updated_at=? WHERE speaker_id=?",
                (name, _now_iso(), speaker_id),
            )
            self._conn.commit()

    def update_enrollment(self, speaker_id, status):
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET enrollment_status=?, updated_at=? WHERE speaker_id=?",
                (status, _now_iso(), speaker_id),
            )
            self._conn.commit()

    def delete_speaker(self, speaker_id):
        with self._lock:
            self._conn.execute("DELETE FROM speaker_embeddings WHERE speaker_id=?",
                               (speaker_id,))
            self._conn.execute("DELETE FROM speakers WHERE speaker_id=?", (speaker_id,))
            self._conn.commit()