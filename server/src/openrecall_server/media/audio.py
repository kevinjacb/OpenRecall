"""Slot-indexed audio persistence — the frame log and peak array (spec §3.1).

Today nothing survives ingest: frames are decoded, fed to the transcriber, and
the PCM is discarded. :class:`~openrecall_server.media.blob.BlobStore` is
content-addressed with no append, which is the wrong shape for a growing
stream, so audio gets its own store.

**Two decisions shape this file.**

*The container is built on read, not on write (D4).* What lands on disk is a
length-prefixed log of raw Opus frames. An Ogg muxer on the ingest path would
put RFC 7845 headers, page CRCs and granule bookkeeping where a bug destroys
audio at write time and you find out days later. Here a muxer bug is
recoverable: delete the cache, fix it, serve again.

*Session time is the master clock (D5).* Slot ``i`` covers
``[i*20, (i+1)*20)`` ms of session time — **always**, with no exceptions for
loss or silence. This is the invariant the whole audio plane rests on: the
Recording Detail page scrubs a waveform against transcript ``start_ms``, and
if bytes-written ever stands in for time-elapsed, the playhead drifts away
from the transcript and the page's core interaction quietly breaks.

Two things would break it if left alone — the reassembler drops late and
duplicate packets, and firmware VAD suppresses silence entirely — so silence
and loss are written explicitly as run-length gap records:

    [u16 len][payload]        len = 1..0xFFFE  -> one Opus frame, one slot
    [u16 0xFFFF][u32 count]   -> a run of `count` silent slots

Run-length encoding matters here because an always-on wearable is mostly
silent: a gap costs 6 bytes however long it is, instead of ~5 bytes per 20 ms.

The peak array is a parallel fixed-stride file, one ``u8`` per slot, gaps
zero. Fixed stride is what makes a segment's waveform a slice rather than a
scan.
"""
from __future__ import annotations

import logging
import os
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

FRAME_MS = 20  # one Opus frame == one slot == 20 ms (V1 audio spec)
GAP_MARKER = 0xFFFF
MAX_FRAME_BYTES = GAP_MARKER - 1

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")


@dataclass(frozen=True)
class AudioStat:
    slot_count: int = 0
    byte_count: int = 0

    @property
    def duration_ms(self) -> int:
        return self.slot_count * FRAME_MS


def _ms_to_slot(ms: int) -> int:
    return max(0, ms // FRAME_MS)


class AudioStore:
    """Per-session Opus frame log + peak array under ``<root>/``.

    One file pair per session: ``<session_id>.opusraw`` and
    ``<session_id>.peaks``. Session ids come from the relay and are used as
    filenames, so they are sanitised — a session id is untrusted input and
    ``../`` in a filename is a directory traversal.

    Thread-safe: the gateway writes from a worker thread while HTTP handlers
    read on the event loop.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # session_id -> slots written so far. Populated lazily by scanning the
        # log, then maintained in memory. The file is the source of truth; this
        # only saves a rescan per append.
        self._slots: dict[str, int] = {}

    # -- paths ---------------------------------------------------------------

    @staticmethod
    def _safe(session_id: str) -> str:
        """Reduce a session id to a filename that cannot escape the root."""
        cleaned = "".join(
            c if (c.isalnum() or c in "-_.") else "_" for c in session_id
        )
        cleaned = cleaned.strip(".") or "unnamed"
        return cleaned[:128]

    def log_path(self, session_id: str) -> Path:
        return self._root / f"{self._safe(session_id)}.opusraw"

    def peaks_path(self, session_id: str) -> Path:
        return self._root / f"{self._safe(session_id)}.peaks"

    def has(self, session_id: str) -> bool:
        path = self.log_path(session_id)
        return path.exists() and path.stat().st_size > 0

    def sessions(self) -> list[str]:
        """Sanitised session names that have a frame log (for the retention
        sweep). These are filename-safe forms, not necessarily the original
        ids — the sweep only needs to delete by them."""
        return sorted(p.stem for p in self._root.glob("*.opusraw"))

    # -- writing -------------------------------------------------------------

    def slot_count(self, session_id: str) -> int:
        with self._lock:
            return self._slot_count_locked(session_id)

    def _slot_count_locked(self, session_id: str) -> int:
        cached = self._slots.get(session_id)
        if cached is not None:
            return cached
        count = 0
        path = self.log_path(session_id)
        if path.exists():
            with path.open("rb") as fh:
                for _slot, _frame in _iter_slots(fh):
                    count += 1
        self._slots[session_id] = count
        return count

    def write_at(
        self,
        session_id: str,
        slot: int,
        frames: list[bytes],
        peaks: list[int] | None = None,
    ) -> int:
        """Place ``frames`` at session-time ``slot``. Returns frames written.

        This is the only writer, and it is where the D5 invariant is enforced:

        * ``slot`` **ahead** of the cursor — the difference is silence or loss.
          A gap record covers it, so the next frame still lands at its true
          session time.
        * ``slot`` **behind** the cursor — a late or duplicate packet, arriving
          after we already wrote a gap over its slot. It is dropped. An
          append-only log cannot rewrite history, and the alternative (append
          it anyway) would shift every later frame's timestamp, which is
          exactly the drift this design exists to prevent. The transcriber
          upstream drops these packets too, so the two stay consistent.
        """
        if not frames:
            # A gap-marker packet carries no audio but does carry time. Move
            # the cursor so the silence it represents is recorded.
            self._advance_to(session_id, slot)
            return 0
        with self._lock:
            cursor = self._slot_count_locked(session_id)
            if slot < cursor:
                log.debug(
                    "audio_write_late session=%s slot=%d cursor=%d — dropped",
                    session_id, slot, cursor,
                )
                return 0
            payload = bytearray()
            if slot > cursor:
                payload += _gap_record(slot - cursor)
            written = 0
            for frame in frames:
                if not frame or len(frame) > MAX_FRAME_BYTES:
                    # A zero-length frame is indistinguishable from a record
                    # boundary and an oversized one collides with the gap
                    # marker. Substitute a one-slot gap so the slot still
                    # exists and the timeline does not shift.
                    payload += _gap_record(1)
                    continue
                payload += _U16.pack(len(frame)) + frame
                written += 1
            with self.log_path(session_id).open("ab") as fh:
                fh.write(bytes(payload))
            self._slots[session_id] = slot + len(frames)
            self._write_peaks(session_id, slot, frames, peaks)
            return written

    def _advance_to(self, session_id: str, slot: int) -> None:
        with self._lock:
            cursor = self._slot_count_locked(session_id)
            if slot <= cursor:
                return
            with self.log_path(session_id).open("ab") as fh:
                fh.write(_gap_record(slot - cursor))
            self._slots[session_id] = slot
            self._pad_peaks(session_id, slot)

    def mark_gap(self, session_id: str, count: int) -> None:
        """Record ``count`` silent slots at the cursor."""
        if count <= 0:
            return
        self._advance_to(session_id, self.slot_count(session_id) + count)

    def stat(self, session_id: str) -> AudioStat:
        path = self.log_path(session_id)
        if not path.exists():
            return AudioStat()
        return AudioStat(
            slot_count=self.slot_count(session_id),
            byte_count=path.stat().st_size,
        )

    # -- peaks ---------------------------------------------------------------

    def _pad_peaks(self, session_id: str, upto_slot: int) -> None:
        """Extend the peak file with zeros so slot N is always at byte N."""
        path = self.peaks_path(session_id)
        current = path.stat().st_size if path.exists() else 0
        if upto_slot > current:
            with path.open("ab") as fh:
                fh.write(b"\x00" * (upto_slot - current))

    def _write_peaks(
        self, session_id: str, slot: int, frames: list[bytes], peaks: list[int] | None,
    ) -> None:
        self._pad_peaks(session_id, slot)
        values = peaks if peaks is not None else [0] * len(frames)
        # Never let a short/long peak list desynchronise the fixed stride —
        # the whole point of this file is that byte N is slot N.
        values = (values + [0] * len(frames))[:len(frames)]
        with self.peaks_path(session_id).open("ab") as fh:
            fh.write(bytes(max(0, min(255, int(v))) for v in values))

    def peaks(self, session_id: str, start_ms: int, end_ms: int) -> list[int]:
        """Per-slot peaks over ``[start_ms, end_ms)``, zero-padded.

        A slice, not a scan: the fixed stride means the file offset *is* the
        slot index.
        """
        path = self.peaks_path(session_id)
        if not path.exists():
            return []
        first, last = _ms_to_slot(start_ms), _ms_to_slot(end_ms)
        if last <= first:
            return []
        with path.open("rb") as fh:
            fh.seek(first)
            raw = fh.read(last - first)
        return list(raw) + [0] * ((last - first) - len(raw))

    # -- reading -------------------------------------------------------------

    def read_range(
        self, session_id: str, start_ms: int, end_ms: int,
    ) -> Iterator[bytes]:
        """Yield one entry per slot in ``[start_ms, end_ms)``.

        A gap slot yields ``b""``. The caller decides what silence means —
        the muxer substitutes a silent frame so the timeline holds; a
        consumer that only wants audio can filter the empties.

        Linear from the start of the log: records are variable length, so
        there is no seek. Acceptable at this scale (a speech-only hour is
        ~10 MB) and the honest alternative — a sidecar offset index — is
        another file to keep consistent with the log.
        """
        path = self.log_path(session_id)
        if not path.exists():
            return
        first, last = _ms_to_slot(start_ms), _ms_to_slot(end_ms)
        with path.open("rb") as fh:
            for slot, frame in _iter_slots(fh):
                if slot >= last:
                    return
                if slot >= first:
                    yield frame

    # -- deletion ------------------------------------------------------------

    def erase_range(self, session_id: str, start_ms: int, end_ms: int) -> int:
        """Replace a slot range with silence, preserving the D5 invariant.

        Rewrites the log rather than truncating it: deleting one recording
        must not shift the timestamps of every recording after it in the same
        session. Returns the number of frames erased.

        Written to a temporary file and renamed, so an interrupted delete
        leaves the original log intact rather than a half-rewritten one.
        """
        path = self.log_path(session_id)
        if not path.exists():
            return 0
        first, last = _ms_to_slot(start_ms), _ms_to_slot(end_ms)
        tmp = path.with_suffix(".opusraw.tmp")
        erased = 0
        with self._lock:
            with path.open("rb") as src, tmp.open("wb") as dst:
                pending_gap = 0
                for slot, frame in _iter_slots(src):
                    if first <= slot < last:
                        pending_gap += 1
                        if frame:
                            erased += 1
                        continue
                    if not frame:
                        pending_gap += 1
                        continue
                    if pending_gap:
                        dst.write(_gap_record(pending_gap))
                        pending_gap = 0
                    dst.write(_U16.pack(len(frame)) + frame)
                if pending_gap:
                    dst.write(_gap_record(pending_gap))
            os.replace(tmp, path)
            self._slots.pop(session_id, None)
        self._zero_peaks(session_id, first, last)
        return erased

    def _zero_peaks(self, session_id: str, first_slot: int, last_slot: int) -> None:
        path = self.peaks_path(session_id)
        if not path.exists() or last_slot <= first_slot:
            return
        size = path.stat().st_size
        if first_slot >= size:
            return
        with path.open("r+b") as fh:
            fh.seek(first_slot)
            fh.write(b"\x00" * (min(last_slot, size) - first_slot))

    def delete(self, session_id: str) -> bool:
        """Remove a session's audio entirely. Idempotent."""
        removed = False
        with self._lock:
            for path in (self.log_path(session_id), self.peaks_path(session_id)):
                if path.exists():
                    path.unlink()
                    removed = True
            self._slots.pop(session_id, None)
        return removed


def _gap_record(count: int) -> bytes:
    return _U16.pack(GAP_MARKER) + _U32.pack(count)


def _iter_slots(fh) -> Iterator[tuple[int, bytes]]:
    """Walk a frame log, yielding ``(slot_index, frame_or_empty)``.

    Gap runs are expanded to one entry per slot, which is what makes slot
    index and file position independent — the caller never has to know how
    silence was encoded.

    A truncated tail (a crash mid-append) ends the iteration quietly. The log
    is append-only, so a partial record can only ever be the last one, and
    losing it costs 20 ms of audio rather than the whole recording.
    """
    slot = 0
    while True:
        header = fh.read(_U16.size)
        if len(header) < _U16.size:
            return
        (length,) = _U16.unpack(header)
        if length == GAP_MARKER:
            raw = fh.read(_U32.size)
            if len(raw) < _U32.size:
                return
            (count,) = _U32.unpack(raw)
            for _ in range(count):
                yield slot, b""
                slot += 1
            continue
        frame = fh.read(length)
        if len(frame) < length:
            return
        yield slot, frame
        slot += 1
