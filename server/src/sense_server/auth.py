# server/src/sense_server/auth.py
from __future__ import annotations
import os
import secrets
from pathlib import Path


def load_or_create_token(path: str | Path) -> str:
    """Return a 32-byte (64 hex char) bearer token, generating + persisting on first use."""
    token_path = Path(path)
    if token_path.exists():
        return token_path.read_text().strip()
    token = secrets.token_hex(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token)
    try:
        token_path.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return token