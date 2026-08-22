"""Content-addressed media blob store.

Photos and video are stored by their sha256 and referenced by hash everywhere else,
so media is never inlined into the event/atom stream. Content addressing makes
``put`` idempotent and the store tamper-evident. Two backends behind one
:class:`BlobStore` protocol: in-memory and a durable filesystem store.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol, runtime_checkable


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class BlobStore(Protocol):
    def put(self, data: bytes) -> str:
        """Store bytes; return their sha256 hex digest (idempotent)."""
        ...

    def get(self, digest: str) -> bytes:
        """Return the bytes for a digest; raise KeyError if absent."""
        ...

    def has(self, digest: str) -> bool: ...

    def delete(self, digest: str) -> None:
        """Remove a blob. Idempotent: no error if the digest is absent."""
        ...


class InMemoryBlobStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        self._blobs.setdefault(digest, data)
        return digest

    def get(self, digest: str) -> bytes:
        try:
            return self._blobs[digest]
        except KeyError:
            raise KeyError(digest) from None

    def has(self, digest: str) -> bool:
        return digest in self._blobs

    def delete(self, digest: str) -> None:
        self._blobs.pop(digest, None)


class FilesystemBlobStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        return self._root / digest

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        path = self._path(digest)
        if not path.exists():  # content-addressed: identical bytes already stored
            path.write_bytes(data)
        return digest

    def get(self, digest: str) -> bytes:
        path = self._path(digest)
        if not path.exists():
            raise KeyError(digest)
        return path.read_bytes()

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()

    def delete(self, digest: str) -> None:
        try:
            self._path(digest).unlink(missing_ok=True)
        except IsADirectoryError:
            pass
