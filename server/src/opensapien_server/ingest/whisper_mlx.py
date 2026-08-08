"""Real MLX-whisper transcriber (Apple Silicon / Mac).

Implements the :class:`~opensapien_server.ingest.transcriber.Transcriber` protocol using
``mlx-whisper``. Kept out of the import path of the tested pipeline core: ``mlx`` and
``numpy`` are imported lazily inside methods, so unit tests never load them.

Install the extra and run on the Mac:

    pip install -e '.[mlx]'

Default model is ``whisper-large-v3-turbo`` — a strong accuracy/speed point on
Apple Silicon with 48 GB. Swap to a distilled/medium model if you need a faster RTF.
"""

from __future__ import annotations

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

# int16 full-scale; PCM bytes -> float32 in [-1, 1) for Whisper.
_INT16_FULL_SCALE = 32768.0


class MlxWhisperTranscriber:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model = model

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        import mlx_whisper
        import numpy as np

        if sample_rate != 16000:
            # Whisper operates at 16 kHz; the V1 audio spec is already 16 kHz, so we
            # refuse rather than silently mis-transcribe a wrong-rate buffer.
            raise ValueError(f"expected 16 kHz PCM, got {sample_rate} Hz")

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE
        result = mlx_whisper.transcribe(audio, path_or_hf_repo=self._model)
        print(f"MLX-whisper transcribed {len(audio)} samples -> {len(result['text'])} chars")
        return result["text"].strip()
