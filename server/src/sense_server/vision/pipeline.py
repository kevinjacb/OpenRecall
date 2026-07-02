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
        session_id: str,
        image: bytes,
        captured_at_ms: int,
        media_type: str = "image/jpeg",
    ) -> MemoryAtom | None:
        """Caption and store an image as a scene atom; None if already captured."""
        digest = sha256_hex(image)
        atom_id = f"{session_id}:scene:{digest}"
        if self._atoms.has(atom_id):
            return None  # already captured for this session — don't re-call the model

        self._blobs.put(image)
        caption = self._vision.caption(image, media_type=media_type)
        atom = MemoryAtom(
            atom_id=atom_id,
            session_id=session_id,
            source_event_id=f"blob:{digest}",  # provenance to the content-addressed media
            kind="scene",
            text=caption,
            created_at=self._clock(),
            start_ms=captured_at_ms,
        )
        self._atoms.append(atom)
        return atom
