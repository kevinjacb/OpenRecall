"""The inference wire format: plain JSON, no third-party types.

PCM is base64 because JSON has no byte type. Token is mirrored field-for-field
rather than pickled so the service can be reimplemented in another language or
runtime without matching Python's object layout.
"""
from __future__ import annotations

import base64

from ..ingest.streaming_transcriber import Token


def encode_pcm(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def decode_pcm(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def token_to_json(t: Token) -> dict:
    return {
        "text": t.text,
        "start_ms": t.start_ms,
        "end_ms": t.end_ms,
        "sentence_id": t.sentence_id,
    }


def token_from_json(d: dict) -> Token:
    return Token(
        text=d["text"],
        start_ms=int(d["start_ms"]),
        end_ms=int(d["end_ms"]),
        sentence_id=int(d.get("sentence_id", 0)),
    )
