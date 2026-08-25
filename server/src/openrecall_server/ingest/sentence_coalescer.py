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

  1. **Model sentence boundary (primary)** — the ASR backend tags each
     token with its sentence id (> 0); the streamer emits one Segment per
     backend sentence, carrying that id. When the id changes between the
     pending run and a new segment, the model closed the pending
     sentence — emit it. This uses the model's own segmentation, which
     handles a mid-sentence period ("Dr. Smith went home.") and
     unpunctuated speech correctly, where the heuristics below get it
     wrong.
  2. **Punctuation** — a segment whose text ends with a sentence
     terminator (``. ! ?``). The fallback boundary for backends with no
     sentence structure (id 0, e.g. the str-returning adapter) and a
     reinforcement for a final punctuation-bearing segment.
  3. **Inter-word pause** — a gap of ``>= pause_ms`` between the end of
     the last pending segment and the start of the next one. Catches the
     end of an utterance that was not punctuated; also the fallback when
     either side has no sentence id.
  4. **Trailing silence** — the audio clock has advanced ``>= pause_ms``
     past the last pending segment's end with no new segment arriving.
     This is what flushes the *last* sentence of an utterance, which
     otherwise waits for a next word that never comes (until session
     end). The audio clock is derived from the PCM fed in, so it
     advances even on silent hops.
  5. **Safety cap** — ``MAX_SENTENCE_WORDS`` or ``MAX_SENTENCE_MS`` to
     bound a run-on with no punctuation and no pause. Also bounds the
     rare cross-hop re-segmentation case where the model merges two
     sentences into one id (under-segmentation degrades to the cap).
  6. **Flush** — ``flush()`` (session end) emits whatever is pending.

A ``sentence_id`` of 0 means "no structure" (the str-returning adapter,
or a backend that didn't expose sentence boundaries): the model boundary
(1) is skipped and the coalescer falls back to punctuation (2) / pause
(3) — the original heuristic behavior.

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
# without punctuation. 400 ms is a comfortable inter-sentence pause; below
# it, words flow as one sentence. Tunable via OPENRECALL_SENTENCE_PAUSE_MS.
DEFAULT_PAUSE_MS = 400

# Safety caps so a run-on with no punctuation and no pause cannot grow a
# pending sentence without bound. These are deliberately generous — a
# normal sentence is well under both.
MAX_SENTENCE_WORDS = 40
MAX_SENTENCE_MS = 10_000

# Characters that end a sentence. Commas / semicolons are clause breaks,
# not sentence breaks, so they do not trigger a boundary.
_SENTENCE_TERMINATORS = (".", "!", "?")


def _ends_sentence(text: str) -> bool:
    """True if ``text`` (stripped) ends with a sentence terminator.

    Handles both an attached terminator (``"hello."``) and a standalone
    punctuation token (``"."``). Whitespace is stripped first.
    """
    return text.rstrip().endswith(_SENTENCE_TERMINATORS)


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
                boundary = False
                # 1. Model sentence boundary (PRIMARY): both the pending run
                #    and this segment carry a real sentence id (> 0), and the
                #    id changed — the ASR backend closed the pending sentence.
                #    This uses the model's own segmentation, which handles a
                #    mid-sentence period ("Dr. Smith went home.") and
                #    unpunctuated speech correctly, where the heuristics below
                #    get it wrong. When the model provides structure it is
                #    authoritative: punctuation is suppressed (step 3) and the
                #    pause heuristic (step 2) is skipped.
                if (
                    self._pending_sentence_id != 0
                    and seg.sentence_id != 0
                    and seg.sentence_id != self._pending_sentence_id
                ):
                    boundary = True
                # 2. Inter-word pause (fallback): reached only when at least
                #    one side has no sentence structure (id 0). A gap >= pause_ms
                #    before this segment means the pending words are a
                #    complete sentence.
                elif self._pending_sentence_id == 0 or seg.sentence_id == 0:
                    gap = seg.start_ms - self._pending[-1].end_ms
                    if gap >= self._pause_ms:
                        boundary = True
                if boundary:
                    out.extend(self._emit_pending())
            self._pending.append(seg)
            if len(self._pending) == 1:
                # First segment of a fresh run sets its sentence id.
                self._pending_sentence_id = seg.sentence_id
            # 3. Punctuation (fallback): this segment ends a sentence. ONLY
            #    in the no-structure path (id 0): when the model provides
            #    sentence ids, its id-change boundary (step 1) is authoritative
            #    and a mid-sentence period ("Dr.") must NOT close the sentence.
            #    The model closes it via the next hop's id change (or flush).
            if self._pending_sentence_id == 0 and _ends_sentence(seg.text):
                out.extend(self._emit_pending())
                continue
            # 4. Safety cap (always applies — bounds even a model that merges
            #    many sentences into one id across hops).
            if self._cap_reached():
                out.extend(self._emit_pending())
                continue
        # 5. Trailing silence: audio has advanced >= pause_ms past the
        #    last pending segment with no new segment arriving. Only
        #    evaluated mid-stream; finalize (6) handles session end.
        if not finalize and self._pending:
            if self._audio_ms - self._pending[-1].end_ms >= self._pause_ms:
                out.extend(self._emit_pending())
        # 6. Finalize: emit whatever is pending at session end.
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