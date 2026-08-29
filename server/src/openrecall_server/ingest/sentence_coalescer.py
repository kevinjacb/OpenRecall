"""Sentence coalescer — groups per-hop word Segments into sentences.

The :class:`StreamingTranscriber` emits one Segment per hop. At a low
latency hop (e.g. 240 ms) each hop advances the committed cursor past
roughly one word, so each hop emits a ~one-word Segment, and the gateway
emits one Transcript per word — every word lands on its own line and is
stored as its own event. That is correct for latency but wrong for
readability and semantics: speech is sentences, not words.

:class:`SentenceCoalescer` wraps a streamer and fixes that without
touching the streamer's delicate committed-cursor / overlap-dedup logic.
It accumulates the streamer's word Segments and emits a single sentence
Segment when a boundary is detected:

  1. **Punctuation** — a segment whose text ends with a sentence
     terminator (``. ! ?``), unless the terminating word is a common
     abbreviation ("Dr.", "Mr.", a single initial) — those are
     mid-sentence periods, not boundaries.
  2. **Inter-word pause** — a gap of ``>= pause_ms`` between the end of
     the last pending segment and the start of the next one. Catches the
     end of an utterance that was not punctuated.
  3. **Trailing silence** — the audio clock has advanced ``>= pause_ms``
     past the last pending segment's end with no new segment arriving.
     This is what flushes the *last* sentence of an utterance, which
     otherwise waits for a next word that never comes (until session
     end). The audio clock is derived from the PCM fed in, so it
     advances even on silent hops.
  4. **Safety cap** — ``MAX_SENTENCE_WORDS`` or ``MAX_SENTENCE_MS`` to
     bound a run-on with no punctuation and no pause.
  5. **Flush** — ``flush()`` (session end) emits whatever is pending.

``sentence_id`` is deliberately IGNORED for boundary decisions. Both
production backends stamp it with the segment's index *within one
transcribe() call* (``seg_idx + 1``), and the streamer calls the backend
once per hop on a rolling window — so the id is call-local and carries no
meaning across hops. Treating it as a stable model boundary (as an
earlier revision did) produced both failure modes at once: continuous
speech transcribed as one segment per window kept id 1 forever, so
punctuation was suppressed and sentences merged into 40-word run-ons;
and whenever the window's segment count shifted between hops, the id
changed mid-sentence and split it at a random word. The id is still
carried on the emitted sentence Segment for observability, nothing more.

The emitted sentence Segment spans the whole sentence
(``start_ms`` of the first word, ``end_ms`` of the last), so the
downstream Transcript's ``duration_ms`` is the sentence's audio span.

Latency trade-off (accepted by design): a sentence appears ~one hop
after its last word, not per-word. This is how live captioning normally
works — sentences, not a word trickle.

The coalescer is a duck-typed stand-in for the streamer: it exposes
``feed`` / ``flush`` / ``_hop_ms`` / ``committed_ms`` so the pipeline can
hold it in ``self._streamer`` without knowing whether it is the raw
streamer or this wrapper.
"""
from __future__ import annotations

from .streaming_transcriber import Segment, StreamingTranscriber

# A gap between consecutive words this wide is a sentence boundary even
# without punctuation. 1000 ms is a reliable inter-sentence pause; below it,
# words flow as one sentence. The previous 400 ms fired inside a natural
# thinking pause and split mid-thought utterances. Tunable via
# OPENRECALL_SENTENCE_PAUSE_MS.
DEFAULT_PAUSE_MS = 1000

# Safety caps so a run-on with no punctuation and no pause cannot grow a
# pending sentence without bound. These are deliberately generous — a
# normal sentence is well under both.
MAX_SENTENCE_WORDS = 40
MAX_SENTENCE_MS = 10_000

# Characters that end a sentence. Commas / semicolons are clause breaks,
# not sentence breaks, so they do not trigger a boundary.
_SENTENCE_TERMINATORS = (".", "!", "?")

# Period-bearing words that are almost always mid-sentence: honorifics and
# similar abbreviations. A sentence genuinely ending in one of these is rare,
# and the pause / trailing-silence boundaries still close it, so the cost of
# a false hold is one merged clause, while a false split cuts "Dr. Smith"
# in half. Lowercased for the comparison.
_ABBREVIATIONS = frozenset({
    "mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "sr.", "jr.",
    "vs.", "e.g.", "i.e.", "no.",
})


def _ends_sentence(text: str) -> bool:
    """True if ``text`` (stripped) ends with a sentence terminator.

    Handles both an attached terminator (``"hello."``) and a standalone
    punctuation token (``"."``). Whitespace is stripped first. A trailing
    abbreviation ("Dr.", "e.g.") or single initial ("J.") is a mid-sentence
    period, not a boundary.
    """
    stripped = text.rstrip()
    if not stripped.endswith(_SENTENCE_TERMINATORS):
        return False
    last = stripped.split()[-1].lower()
    if last in _ABBREVIATIONS:
        return False
    # A single initial ("J.") — one letter plus a period.
    if len(last) == 2 and last[0].isalpha() and last[1] == ".":
        return False
    return True


def _word_count(text: str) -> int:
    return len(text.split())


class SentenceCoalescer:
    """Wrap a :class:`StreamingTranscriber` to emit sentence-level Segments.

    See the module docstring for the boundary model and the latency
    trade-off. The coalescer is constructed by the pipeline when sentence
    coalescing is enabled; it is never used directly by application code.
    """

    def __init__(
        self,
        streamer: StreamingTranscriber,
        sample_rate: int = 16000,
        pause_ms: int = DEFAULT_PAUSE_MS,
    ) -> None:
        self._streamer = streamer
        self._sample_rate = sample_rate
        self._pause_ms = pause_ms
        # The pipeline reads ``self._streamer._hop_ms``; mirror it so the
        # coalescer is a transparent stand-in for the raw streamer.
        self._hop_ms = streamer._hop_ms
        # Word Segments accumulated since the last sentence boundary.
        self._pending: list[Segment] = []
        # The sentence_id of the current pending run (0 = no structure, so
        # heuristics decide boundaries; > 0 = a real backend sentence, so the
        # model's boundary — an id change — decides). Captured from the first
        # segment of each run.
        self._pending_sentence_id: int = 0
        # Cumulative audio time fed (ms). Advances on every feed(), even
        # on silent hops, so trailing-silence can be detected without a
        # new segment arriving.
        self._audio_ms: int = 0

    # ------------------------------------------------------------------
    # Streamer stand-in surface
    # ------------------------------------------------------------------
    @property
    def committed_ms(self) -> int:
        return self._streamer.committed_ms

    def feed(self, pcm: bytes) -> list[Segment]:
        if pcm:
            self._audio_ms += len(pcm) * 1000 // (self._sample_rate * 2)
        raw = self._streamer.feed(pcm)
        return self._absorb(raw, finalize=False)

    def flush(self) -> list[Segment]:
        raw = self._streamer.flush()
        return self._absorb(raw, finalize=True)

    # ------------------------------------------------------------------
    # Coalescing
    # ------------------------------------------------------------------
    def _absorb(self, raw: list[Segment], *, finalize: bool) -> list[Segment]:
        out: list[Segment] = []
        for seg in raw:
            if self._pending:
                # 1. Inter-word pause: a gap >= pause_ms before this segment
                #    means the pending words are a complete sentence. The
                #    sentence_id is call-local (see module docstring) and is
                #    deliberately NOT consulted.
                gap = seg.start_ms - self._pending[-1].end_ms
                if gap >= self._pause_ms:
                    out.extend(self._emit_pending())
            self._pending.append(seg)
            if len(self._pending) == 1:
                # First segment of a fresh run sets its sentence id (carried
                # on the emitted Segment for observability only).
                self._pending_sentence_id = seg.sentence_id
            # 2. Punctuation: this segment ends a sentence (abbreviations and
            #    initials excluded — see _ends_sentence).
            if _ends_sentence(seg.text):
                out.extend(self._emit_pending())
                continue
            # 3. Safety cap — bounds a run-on with no punctuation and no pause.
            if self._cap_reached():
                out.extend(self._emit_pending())
                continue
        # 4. Trailing silence: audio has advanced >= pause_ms past the
        #    last pending segment with no new segment arriving. Only
        #    evaluated mid-stream; finalize (5) handles session end.
        if not finalize and self._pending:
            if self._audio_ms - self._pending[-1].end_ms >= self._pause_ms:
                out.extend(self._emit_pending())
        # 5. Finalize: emit whatever is pending at session end.
        if finalize and self._pending:
            out.extend(self._emit_pending())
        return out

    def _emit_pending(self) -> list[Segment]:
        if not self._pending:
            return []
        text = " ".join(s.text for s in self._pending)
        segment = Segment(
            text=text,
            start_ms=self._pending[0].start_ms,
            end_ms=self._pending[-1].end_ms,
            sentence_id=self._pending_sentence_id,
        )
        self._pending = []
        self._pending_sentence_id = 0
        return [segment]

    def _cap_reached(self) -> bool:
        words = sum(_word_count(s.text) for s in self._pending)
        if words >= MAX_SENTENCE_WORDS:
            return True
        if len(self._pending) >= 2:
            span = self._pending[-1].end_ms - self._pending[0].start_ms
            if span >= MAX_SENTENCE_MS:
                return True
        return False