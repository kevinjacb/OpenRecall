#!/usr/bin/env python3
"""Trim/normalize a 16 kHz mono wav to N seconds for the speaker-embedder
integration test fixtures.

LibriSpeech wavs are already 16 kHz mono int16, so this just trims to the
first ``seconds`` and rewrites the header. Usage:

    python tools/make_speaker_fixture.py <in.wav> <out.wav> [seconds]

If the input is not 16 kHz mono, convert it first (e.g. with ffmpeg:
``ffmpeg -i in.wav -ar 16000 -ac 1 out.wav``). LibriSpeech dev-clean ships
``.flac`` files — convert with ``ffmpeg -i in.flac -ar 16000 -ac 1 out.wav``
(or ``soundfile``) before running this tool.
"""
from __future__ import annotations

import sys
import wave


def main(in_path: str, out_path: str, seconds: float = 4.0) -> None:
    with wave.open(in_path, "rb") as r:
        if r.getframerate() != 16000:
            raise SystemExit(f"expected 16 kHz, got {r.getframerate()}")
        if r.getnchannels() != 1:
            raise SystemExit(f"expected mono, got {r.getnchannels()} channels")
        n = int(r.getframerate() * seconds)
        frames = r.readframes(min(n, r.getnframes()))
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(frames)
    print(f"wrote {out_path} ({len(frames)//2} samples)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: make_speaker_fixture.py <in.wav> <out.wav> [seconds]")
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 4.0)