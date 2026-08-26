"""Server-side PCM denoise — spectral gating on the decoded audio before it
reaches the transcriber and the speaker embedder.

Why this exists: the INMP441 MEMS mic has no AGC and no hardware filter, the
firmware ships raw PCM (the dual-mic NLMS canceller is bypassed — two omni mics
with no acoustic baffle would cancel the voice), and nothing filters the
signal end-to-end. So recordings carry the full room tone / HVAC / handling
noise, and that same noisy audio is what the ASR transcribes. Denoising the
decoded PCM is the single biggest background-noise win and lifts transcription
accuracy at the same time.

The firmware VAD tags silence and the reassembler consumes those gap-markers
without emitting audio, so silence frames never reach the pipeline. The noise
profile is therefore self-calibrated from the quietest hops seen (room tone
between words), not from explicit silence. Once enough low-energy audio
accumulates the profile is cached and each hop is gated against it — a fixed
spectral filter per hop, so the per-hop cost is one STFT/ISTFT rather than a
re-estimation.

Off by default (``OPENRECALL_DENOISE_ENABLED=true``); the ``noisereduce`` extra
is lazy-imported so a default install and the unit suite run without it.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# int16 mono PCM. The pipeline's decoder emits 16-bit LE mono at 16 kHz.
_SAMPLE_BYTES = 2


class PcmDenoiser(Protocol):
    """A pass-through or denoising transform on decoded mono int16 PCM bytes."""

    def process(self, pcm: bytes) -> bytes:
        """Return PCM for this chunk (may be unchanged, or denoised)."""
        ...

    def reset(self) -> None:
        """Drop any per-session state (the noise profile) for a fresh stream."""
        ...


class NoopDenoiser:
    """Pass-through: the default when denoise is off."""

    __slots__ = ()

    def process(self, pcm: bytes) -> bytes:
        return pcm

    def reset(self) -> None:
        pass


def _rms_i16(pcm: bytes) -> float:
    """Root-mean-square of int16 PCM, in raw int16 units."""
    import numpy as np

    arr = np.frombuffer(pcm, dtype=np.int16)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


class NoisereduceDenoiser:
    """Spectral-gating denoise via noisereduce, with a self-calibrating noise
    profile.

    While the profile is being built, ``process`` is a pass-through: the first
    ~``profile_ms`` of audio is examined, and if it is quiet enough (RMS below
    ``ambience_rms``) it becomes the cached noise profile. If speech dominates
    from the start the accumulator is halved and re-examined, so the profile is
    taken from the first real pause; if no quiet window ever appears within
    ``max_profile_wait_ms`` a best-effort profile is used (better than never
    denoising, and the gate stays conservative). After that each chunk is gated
    against the cached profile.

    ``reduce_fn`` is injectable for tests (it defaults to the real
    ``noisereduce.reduce_noise``, lazy-imported on first use so the module
    imports without the extra installed).
    """

    def __init__(
        self,
        sample_rate: int,
        *,
        profile_ms: int = 2000,
        max_profile_wait_ms: int = 8000,
        ambience_rms: float = 300.0,
        reduce_fn=None,
    ) -> None:
        self._sr = sample_rate
        self._profile = None  # numpy float32 noise sample
        self._acc: bytearray = bytearray()
        self._acc_target = sample_rate * _SAMPLE_BYTES * profile_ms // 1000
        self._acc_max = sample_rate * _SAMPLE_BYTES * max_profile_wait_ms // 1000
        # Total bytes seen since reset, monotonically increasing — unlike the
        # accumulator (which is halved on a loud window), this is what the
        # max-wait ceiling is measured against. Otherwise continuous loud audio
        # keeps the accumulator pinned near one chunk and the best-effort
        # commit never fires.
        self._total_seen = 0
        self._ambience_rms = ambience_rms
        self._reduce_fn = reduce_fn  # None -> lazy import noisereduce on first denoise

    def process(self, pcm: bytes) -> bytes:
        if self._profile is None:
            return self._accumulate(pcm)
        return self._denoise(pcm)

    def reset(self) -> None:
        self._profile = None
        self._acc = bytearray()
        self._total_seen = 0

    def _accumulate(self, pcm: bytes) -> bytes:
        self._acc.extend(pcm)
        self._total_seen += len(pcm)
        if len(self._acc) < self._acc_target:
            return pcm
        # Enough audio to evaluate; keep it only if it is genuinely quiet.
        if _rms_i16(bytes(self._acc)) <= self._ambience_rms:
            self._commit_profile()
        else:
            # Speech is dominating the accumulation. Drop the head so newer
            # (hopefully quieter) audio can replace it, and keep waiting.
            del self._acc[: len(self._acc) // 2]
            if self._total_seen >= self._acc_max:
                logger.warning(
                    "denoise: no quiet window in %d ms; using best-effort profile",
                    self._acc_max * 1000 // (self._sr * _SAMPLE_BYTES),
                )
                self._commit_profile()
        # Passthrough until the profile is committed (in-flight audio is left
        # untouched rather than partially denoised).
        return pcm

    def _commit_profile(self) -> None:
        import numpy as np

        self._profile = np.frombuffer(bytes(self._acc), dtype=np.int16).astype(np.float32)
        logger.info(
            "denoise: noise profile ready (%d ms, rms=%.0f)",
            len(self._profile) * 1000 // (self._sr * _SAMPLE_BYTES),
            _rms_i16(bytes(self._acc)),
        )
        self._acc = bytearray()

    def _denoise(self, pcm: bytes) -> bytes:
        import numpy as np

        reduce_fn = self._reduce_fn
        if reduce_fn is None:
            import noisereduce as nr

            reduce_fn = nr.reduce_noise
        y = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        # stationary=True: we have a cached noise profile, so apply a fixed
        # spectral gate against it rather than re-estimating noise per chunk.
        denoised = reduce_fn(y, self._sr, y_noise=self._profile, stationary=True)
        # noisereduce returns float; clamp to int16 range and round.
        out = np.clip(np.asarray(denoised, dtype=np.float32), -32768.0, 32767.0)
        return out.astype(np.int16).tobytes()


def build_denoiser(
    *, enabled: bool, sample_rate: int, reduce_fn=None,
) -> PcmDenoiser:
    """Construct the configured denoiser; ``NoopDenoiser`` when off (the
    default) so there is zero cost and zero behavior change on a default
    install."""
    if not enabled:
        return NoopDenoiser()
    return NoisereduceDenoiser(sample_rate, reduce_fn=reduce_fn)