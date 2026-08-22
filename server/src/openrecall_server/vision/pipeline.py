"""Vision capture pipeline: image -> stored blob + caption -> scene atom.

Stores the image content-addressed (never inlined), captions it with the pluggable
vision model, and writes a "scene" :class:`MemoryAtom` into the same AtomStore as
audio memories — so vision and audio share one index and one retriever. Capture is
idempotent per session/image (keyed on the image hash), and the model is only
called when an atom doesn't already exist, so re-captures don't burn inference.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ..media.blob import BlobStore, sha256_hex
from ..memory.atom import MemoryAtom
from ..memory.store import AtomStore
from .model import VisionModel

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VisionPipeline:
    def __init__(
        self,
        blob_store: BlobStore,
        vision_model: VisionModel,
        atom_store: AtomStore,
        clock: Clock = _utcnow,
    ) -> None:
        self._blobs = blob_store
        self._vision = vision_model
        self._atoms = atom_store
        self._clock = clock

    def capture(
        self,
        session_id: str | None,
        image: bytes,
        captured_at_ms: int,
        media_type: str = "image/jpeg",
        *,
        occurred_at: datetime | None = None,
    ) -> MemoryAtom | None:
        """Caption and store an image as a scene atom; None if already captured.

        ``session_id`` is None for snapshots that could not be mapped to a
        session (the atom is still useful — its caption is retrievable). The
        caller may supply ``occurred_at`` (the upload route derives it from the
        device rel_ts → session timeline); otherwise capture time is used.
        """
        digest = sha256_hex(image)
        atom_id = f"{session_id}:scene:{digest}" if session_id else f"scene:{digest}"
        if self._atoms.has(atom_id):
            return None  # already captured — don't re-call the model

        self._blobs.put(image)
        caption = self._vision.caption(image, media_type=media_type)
        when = occurred_at if occurred_at is not None else self._clock()
        atom = MemoryAtom(
            atom_id=atom_id,
            session_id=session_id,
            source_event_id=f"blob:{digest}",  # provenance to the content-addressed media
            kind="scene",
            text=caption,
            created_at=self._clock(),
            # D10: a scene is a vision capture — report source_modality="vision".
            source_pipeline_version="vision",
            occurred_at=when,
            start_ms=captured_at_ms,
        )
        self._atoms.append(atom)
        return atom
