"""Pluggable interfaces for the ingest pipeline's decode + transcribe stages.

These are deliberately tiny ``Protocol``s so the pipeline logic can be unit-tested
with fakes, while the heavyweight real implementations (libopus decode, MLX-whisper)
live behind the same shape and run on the Mac.

PCM convention across the pipeline: 16-bit signed little-endian, mono, at the
pipeline's configured ``sample_rate`` (16 kHz for the V1 audio spec).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Transcript:
    """One transcribed window of audio."""

    text: str
    duration_ms: int


@runtime_checkable
class OpusDecoder(Protocol):
    """Decodes one Opus frame to PCM bytes (16-bit LE mono)."""

    def decode(self, frame: bytes) -> bytes: ...


@runtime_checkable
class Transcriber(Protocol):
    """Transcribes a window of PCM bytes (16-bit LE mono) to text."""

    def transcribe(self, pcm: bytes, sample_rate: int) -> str: ...
