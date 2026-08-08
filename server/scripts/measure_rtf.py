#!/usr/bin/env python3
"""Spike 3/4 server measurement: does Whisper keep up with real time on this Mac?

Measures the *real-time factor* (RTF = wall_time / audio_duration) of the real
MLX-whisper transcriber on fixed-size windows — the actual risk in the Memory Mode
pipeline (Opus decode is cheap; the model is not). RTF < 1 means faster than real
time; for continuous capture you want comfortable headroom (< ~0.5) because the
extraction LLM, vision model, and embeddings share the same machine.

Run on the Mac after installing the extra:

    pip install -e '.[mlx]'
    python scripts/measure_rtf.py --seconds 120            # synthetic audio
    python scripts/measure_rtf.py --wav sample_speech.wav  # real 16 kHz mono wav

Synthetic audio gives a valid *timing* number (compute scales with audio length,
not content); use a real speech wav if you also want to eyeball transcript quality.
"""

from __future__ import annotations

import argparse
import statistics
import time
import wave

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # int16


def load_wav(path: str) -> bytes:
    """Read a 16 kHz mono 16-bit PCM wav into raw int16 LE bytes."""
    with wave.open(path, "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(
                f"need 16 kHz mono 16-bit wav; got {w.getframerate()} Hz, "
                f"{w.getnchannels()} ch, {w.getsampwidth() * 8}-bit"
            )
        return w.readframes(w.getnframes())


def synth_pcm(seconds: int) -> bytes:
    """Generate speech-ish 16 kHz int16 audio (300 Hz tone + noise) of N seconds."""
    import numpy as np

    n = seconds * SAMPLE_RATE
    t = np.arange(n) / SAMPLE_RATE
    tone = 8000.0 * np.sin(2 * np.pi * 300.0 * t)
    noise = np.random.default_rng(0).integers(-8192, 8192, size=n)
    sig = np.clip(tone + noise, -32768, 32767).astype("<i2")
    return sig.tobytes()


def windows(pcm: bytes, window_ms: int) -> list[bytes]:
    step = (window_ms * SAMPLE_RATE // 1000) * BYTES_PER_SAMPLE
    return [pcm[i : i + step] for i in range(0, len(pcm), step) if pcm[i : i + step]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--wav", help="path to a 16 kHz mono 16-bit wav")
    src.add_argument("--seconds", type=int, default=120, help="synthetic audio length")
    ap.add_argument("--window-ms", type=int, default=5000, help="transcription window")
    ap.add_argument("--model", default=None, help="override the MLX-whisper model repo")
    args = ap.parse_args()

    from openrecall_server.ingest.whisper_mlx import DEFAULT_MODEL, MlxWhisperTranscriber

    model = args.model or DEFAULT_MODEL
    pcm = load_wav(args.wav) if args.wav else synth_pcm(args.seconds)
    chunks = windows(pcm, args.window_ms)
    audio_s = len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)

    print(f"model={model}  window={args.window_ms} ms  windows={len(chunks)}  "
          f"audio={audio_s:.1f} s")
    tr = MlxWhisperTranscriber(model=model)

    print("warming up (model load + compile, not timed)…")
    tr.transcribe(chunks[0], SAMPLE_RATE)  # excluded from measurement

    rtfs: list[float] = []
    wall = 0.0
    for i, ch in enumerate(chunks):
        win_s = len(ch) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        t0 = time.perf_counter()
        text = tr.transcribe(ch, SAMPLE_RATE)
        dt = time.perf_counter() - t0
        wall += dt
        rtf = dt / win_s
        rtfs.append(rtf)
        print(f"  win {i:>3}: {dt:6.2f}s for {win_s:4.1f}s audio  RTF={rtf:5.2f}  "
              f"| {text[:60]!r}")

    mean_rtf = wall / audio_s
    p95 = sorted(rtfs)[min(len(rtfs) - 1, int(len(rtfs) * 0.95))]
    print(f"\naggregate: wall={wall:.1f}s  audio={audio_s:.1f}s  "
          f"mean RTF={mean_rtf:.2f}  p95 RTF={p95:.2f}  "
          f"per-window mean={statistics.mean(rtfs):.2f}")
    if mean_rtf < 0.5:
        verdict = "PASS ✅ — comfortable headroom for the rest of the stack"
    elif mean_rtf < 1.0:
        verdict = "MARGINAL ⚠️ — keeps up, but little room for LLM/vision/embeddings"
    else:
        verdict = "FAIL ❌ — slower than real time; smaller model or batching needed"
    print(f">>> WHISPER RTF VERDICT: {verdict}")


if __name__ == "__main__":
    main()
