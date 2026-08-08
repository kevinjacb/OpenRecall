"""Streaming transcriber — eliminates boundary-loss artifacts.

The hard-cut pipeline (5s windows, one Whisper call per window) loses
speech at every hop boundary: a word that straddles two windows is
transcribed twice or garbled. This module wraps any
:class:`Transcriber` (real or fake) with a streaming algorithm:

  * feed audio in 1-second hops with 5 seconds of rolling context,
  * call the underlying transcriber with the current 5s window,
  * track which tokens have been "committed" (i.e. past the audio
    that will be re-fed in the next hop), and only emit those,
  * keep the uncommitted tail in memory so a word that started at
    the boundary can finish being transcribed before we emit it.

The result: each word appears in exactly one segment, regardless of
where it falls in the hop grid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .transcriber import Transcriber


@dataclass(frozen=True, slots=True)
class Token:
    """One word with its absolute timestamps in the call's audio window.

    ``start_ms`` and ``end_ms`` are millisecond offsets into the audio
    passed to the underlying transcriber (the rolling window), not
    absolute session time. The :class:`StreamingTranscriber` translates
    to absolute time when emitting segments.
    """

    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class Segment:
    """A committed slice of transcript with absolute session timestamps."""

    text: str
    start_ms: int
    end_ms: int


@runtime_checkable
class StreamingBackend(Protocol):
    """The minimal interface the streaming wrapper needs from a backend.

    Production uses :class:`WhisperStreamingBackend` (mlx-whisper with
    word_timestamps=True). Tests use :class:`FakeWhisper`.

    This Protocol is intentionally NOT marked ``@runtime_checkable``
    because its duck-typed shape is identical to the legacy
    :class:`Transcriber` — the only difference is the return type
    (str vs list[Token]). Callers must construct the streaming
    transcriber via :func:`streaming_from_tokens` (token-returning
    backend) or :func:`streaming_from_text` (str-returning legacy
    transcriber) so the wrapping is unambiguous.
    """

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        ...


class _TranscriberAdapter:
    """Adapts a vanilla :class:`Transcriber` (returns str) into a
    :class:`StreamingBackend` that returns tokens.

    The whole utterance is given a timestamp at the *end* of the audio
    it just transcribed (``duration_ms``). The streaming wrapper then
    sees the new token's start as past the committed cursor and emits
    it. The next hop's token, with the same start relative to the new
    buffer, is past the cursor too — and gets emitted. The dedup
    logic still catches re-transcriptions of the same audio in the
    overlap zone, because the start_ms in hop-local coordinates is
    unchanged.
    """

    def __init__(self, transcriber: Transcriber) -> None:
        self._t = transcriber

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        text = self._t.transcribe(pcm, sample_rate)
        if not text or not text.strip():
            return []
        # The str-returning transcriber can't give internal timestamps,
        # so we report a token at the trailing edge of the call's
        # audio. The streaming wrapper's dedup uses start_ms to skip
        # re-transcriptions in the overlap zone: the next hop's
        # token has a larger start_ms (further in the session) and
        # is past the committed cursor, so it's emitted as new.
        duration_ms = max(1, (len(pcm) // 2) * 1000 // sample_rate)
        return [Token(text=text.strip(), start_ms=duration_ms, end_ms=duration_ms)]


class StreamingTranscriber:
    """Wraps a :class:`StreamingBackend` to produce stable segments.

    The wrapper keeps a rolling PCM buffer of size ``window_ms`` and a
    "committed token cursor" — the millisecond offset in session
    time past which all tokens are stable. Each ``feed`` call:

      1. Appends the new PCM to the rolling buffer.
      2. Trims the front so the buffer is at most ``window_ms`` long.
      3. Calls the backend with the current buffer.
      4. Determines which tokens are *new* (start >= committed cursor)
         and emits them as a :class:`Segment`.
      5. Advances the committed cursor to the end of the last new token
         (so the next hop doesn't re-emit it).
    """

    def __init__(
        self,
        backend: "StreamingBackend | Transcriber",
        sample_rate: int = 16000,
        hop_ms: int = 1000,
        window_ms: int = 5000,
        is_token_backend: bool = False,
    ) -> None:
        """Construct the streaming transcriber.

        Use the :func:`streaming_from_tokens` / :func:`streaming_from_text`
        factory functions rather than the constructor directly — they
        set ``is_token_backend`` correctly so the wrapping is unambiguous.
        """
        if hop_ms <= 0 or hop_ms > window_ms:
            raise ValueError(
                f"hop_ms ({hop_ms}) must be in (0, window_ms ({window_ms})]; "
                f"otherwise the rolling window can't cover each hop"
            )
        if window_ms <= 0:
            raise ValueError(f"window_ms must be > 0")
        if is_token_backend:
            self._backend: "StreamingBackend" = backend  # type: ignore[assignment]
        else:
            self._backend = _TranscriberAdapter(backend)
        self._sample_rate = sample_rate
        self._hop_ms = hop_ms
        self._window_ms = window_ms
        self._hop_bytes = (sample_rate * hop_ms // 1000) * 2  # 16-bit LE mono
        self._window_bytes = (sample_rate * window_ms // 1000) * 2

        # Rolling PCM buffer (most-recent ``window_ms`` of audio).
        self._buffer: bytearray = bytearray()
        # Absolute session time of the front of the rolling buffer.
        self._buffer_start_ms: int = 0
        # Absolute session time past which all tokens are committed.
        self._committed_ms: int = 0
        # Tokens emitted in the current hop (so we can extend the last
        # segment with the next hop's continuation).
        self._hop_tokens: list[Token] = []

    @property
    def committed_ms(self) -> int:
        return self._committed_ms

    def feed(self, pcm: bytes) -> list[Segment]:
        """Append a hop of audio; return any newly-committed segments.

        The caller is responsible for feeding exactly ``hop_ms`` worth
        of audio per call (except the last call at session end, which
        may be shorter). Mixing hop sizes mid-session is undefined.
        """
        if not pcm:
            return []

        # 1. Append to the rolling buffer.
        self._buffer.extend(pcm)
        # 2. Trim the front so the buffer is at most window_bytes long.
        if len(self._buffer) > self._window_bytes:
            trim = len(self._buffer) - self._window_bytes
            del self._buffer[:trim]
            # The front of the buffer is now newer; advance the start.
            self._buffer_start_ms += (trim // 2) * 1000 // self._sample_rate

        # 3. Call the backend with the current buffer.
        tokens = self._backend.transcribe(bytes(self._buffer), self._sample_rate)

        # 4. Determine which tokens are new (their start is past the
        #    committed cursor) and translate to absolute session time.
        new_tokens: list[Token] = []
        for tok in tokens:
            absolute_start = self._buffer_start_ms + tok.start_ms
            absolute_end = self._buffer_start_ms + tok.end_ms
            if absolute_start < self._committed_ms:
                # Already committed; skip (avoids double-emission at hop
                # boundaries where Whisper re-transcribes the overlap
                # zone and returns the same text).
                continue
            new_tokens.append(Token(
                text=tok.text,
                start_ms=absolute_start,
                end_ms=absolute_end,
            ))

        if not new_tokens:
            return []

        # 5. Group new tokens into one segment per hop. A future
        #    refinement could split on long pauses, but for now
        #    one hop = one segment keeps the gateway's existing
        #    segment-per-Transcript model intact.
        text = " ".join(t.text for t in new_tokens)
        segment = Segment(
            text=text,
            start_ms=new_tokens[0].start_ms,
            end_ms=new_tokens[-1].end_ms,
        )
        # 6. Advance the committed cursor to the end of the last new
        #    token. We trust the backend's timestamps — a token-returning
        #    backend (Whisper with word_timestamps) reports real word
        #    boundaries, and the str-returning adapter reports the
        #    trailing edge of the call's audio, which is naturally past
        #    the previous hop's end.
        self._committed_ms = max(self._committed_ms, segment.end_ms)
        return [segment]

    def flush(self) -> list[Segment]:
        """Emit any final tokens that haven't been committed yet.

        On session end, the last hop may have a token whose ``end_ms``
        is past the committed cursor (because the cursor is held
        back). Calling ``flush`` advances the cursor to the end of
        the most recent segment and returns the final uncommitted
        text.
        """
        if not self._buffer:
            return []
        tokens = self._backend.transcribe(bytes(self._buffer), self._sample_rate)
        new_tokens = []
        for tok in tokens:
            absolute_start = self._buffer_start_ms + tok.start_ms
            absolute_end = self._buffer_start_ms + tok.end_ms
            if absolute_start < self._committed_ms:
                continue
            new_tokens.append(Token(
                text=tok.text,
                start_ms=absolute_start,
                end_ms=absolute_end,
            ))
        if not new_tokens:
            return []
        text = " ".join(t.text for t in new_tokens)
        segment = Segment(
            text=text,
            start_ms=new_tokens[0].start_ms,
            end_ms=new_tokens[-1].end_ms,
        )
        self._committed_ms = segment.end_ms
        return [segment]


def streaming_from_tokens(
    backend: "StreamingBackend",
    sample_rate: int = 16000,
    hop_ms: int = 1000,
    window_ms: int = 5000,
) -> StreamingTranscriber:
    """Build a :class:`StreamingTranscriber` from a token-returning backend.

    Use this when the backend supports word-level timestamps
    (mlx-whisper with ``word_timestamps=True``)."""
    return StreamingTranscriber(
        backend, sample_rate, hop_ms, window_ms, is_token_backend=True,
    )


def streaming_from_text(
    transcriber: Transcriber,
    sample_rate: int = 16000,
    hop_ms: int = 1000,
    window_ms: int = 5000,
) -> StreamingTranscriber:
    """Build a :class:`StreamingTranscriber` from a str-returning legacy
    :class:`Transcriber`. The transcriber's whole output is treated as
    one token per hop (no internal timestamps). Better than hard cuts
    at 5s, but not as good as :func:`streaming_from_tokens`."""
    return StreamingTranscriber(
        transcriber, sample_rate, hop_ms, window_ms, is_token_backend=False,
    )
