"""Tests for :class:`SentenceCoalescer`.

The coalescer wraps a streamer and groups per-hop word Segments into
sentence Segments. These tests drive it with a fake streamer whose
``feed`` returns scripted Segments, so the boundary logic is exercised
in isolation from the real ASR backend.

PCM fed to ``coalescer.feed`` is sized to one hop so the coalescer's
internal audio clock (``_audio_ms``) advances by ``hop_ms`` per call —
that clock drives the trailing-silence boundary, so the sizes matter.
"""

from openrecall_server.ingest.sentence_coalescer import (
    DEFAULT_PAUSE_MS,
    MAX_SENTENCE_WORDS,
    SentenceCoalescer,
    _ends_sentence,
)
from openrecall_server.ingest.streaming_transcriber import Segment

SAMPLE_RATE = 16000


def _hop_pcm(hop_ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """One hop's worth of silent PCM (16-bit LE mono)."""
    return b"\x00" * ((sample_rate * hop_ms // 1000) * 2)


class FakeStreamer:
    """A streamer stand-in returning scripted Segments per ``feed``.

    ``script`` is a list, one entry per ``feed`` call. Each entry is a
    list of :class:`Segment` (returned verbatim) or ``None`` (the streamer
    had nothing new this hop — silence). Once the script is exhausted,
    further ``feed`` calls return ``[]``. ``flush`` returns nothing by
    default; override ``flush_script`` to change that.
    """

    _hop_ms = 240

    def __init__(self, script, hop_ms=240, flush_script=None):
        self._script = list(script)
        self._hop_ms = hop_ms
        self._flush_script = flush_script or []
        self.committed_ms = 0

    def feed(self, pcm):
        if not self._script:
            return []
        entry = self._script.pop(0)
        return list(entry) if entry is not None else []

    def flush(self):
        return list(self._flush_script)


def _S(text, start, end):
    return Segment(text=text, start_ms=start, end_ms=end)


def _Ss(text, start, end, sid):
    """A Segment tagged with a real backend sentence id (> 0)."""
    return Segment(text=text, start_ms=start, end_ms=end, sentence_id=sid)


# ---------------------------------------------------------------------------
# _ends_sentence helper
# ---------------------------------------------------------------------------
def test_ends_sentence_detects_terminators():
    assert _ends_sentence("hello.")
    assert _ends_sentence("hello!")
    assert _ends_sentence("hello?")
    assert _ends_sentence("world.") is True
    # standalone punctuation token
    assert _ends_sentence(".")
    # trailing whitespace tolerated
    assert _ends_sentence("hello.  ")


def test_ends_sentence_rejects_non_terminators():
    assert not _ends_sentence("hello")
    assert not _ends_sentence("hello,")
    assert not _ends_sentence("hello;")
    assert not _ends_sentence("")


# ---------------------------------------------------------------------------
# Accumulation + punctuation
# ---------------------------------------------------------------------------
def test_words_accumulate_until_punctuation_then_emit_one_sentence():
    s = SentenceCoalescer(FakeStreamer([
        [_S("Hello", 0, 200)],
        [_S("world.", 200, 400)],
    ]))
    out1 = s.feed(_hop_pcm(240))
    out2 = s.feed(_hop_pcm(240))
    # First hop: nothing emitted yet (word held).
    assert out1 == []
    # Second hop: "world." ends the sentence -> emit the whole sentence.
    assert [seg.text for seg in out2] == ["Hello world."]
    assert out2[0].start_ms == 0
    assert out2[0].end_ms == 400


def test_no_punctuation_holds_until_flush():
    s = SentenceCoalescer(FakeStreamer([
        [_S("hello", 0, 200)],
        [_S("world", 200, 400)],
    ]))
    out1 = s.feed(_hop_pcm(240))
    out2 = s.feed(_hop_pcm(240))
    assert out1 == []
    assert out2 == []  # still no boundary
    tail = s.flush()
    assert [seg.text for seg in tail] == ["hello world"]


# ---------------------------------------------------------------------------
# Inter-word pause boundary (the str-adapter / large-hop path)
# ---------------------------------------------------------------------------
def test_inter_word_pause_emits_prior_words_as_sentence():
    # hop=1000: each segment sits at the hop's trailing edge, so the gap
    # between consecutive segments equals hop_ms (1000 >= 400 pause).
    streamer = FakeStreamer(
        [
            [_S("seg1", 1000, 1000)],
            [_S("seg2", 2000, 2000)],
            [_S("seg3", 3000, 3000)],
        ],
        hop_ms=1000,
    )
    s = SentenceCoalescer(streamer, pause_ms=400)
    out1 = s.feed(_hop_pcm(1000))
    out2 = s.feed(_hop_pcm(1000))
    out3 = s.feed(_hop_pcm(1000))
    assert out1 == []                       # seg1 held
    assert [seg.text for seg in out2] == ["seg1"]   # pause -> emit seg1
    assert [seg.text for seg in out3] == ["seg2"]   # pause -> emit seg2
    assert [seg.text for seg in s.flush()] == ["seg3"]


# ---------------------------------------------------------------------------
# Trailing silence (the last sentence of an utterance)
# ---------------------------------------------------------------------------
def test_trailing_silence_flushes_pending_without_a_new_word():
    # One word, then several silent hops advance the audio clock past the
    # pause threshold with no new segment arriving.
    s = SentenceCoalescer(FakeStreamer([
        [_S("hello", 0, 200)],
        None,   # silent hop
        None,   # silent hop
    ]), pause_ms=400)
    s.feed(_hop_pcm(240))   # audio=240, hello.end=200, gap=40  -> hold
    assert s._pending  # still held
    out = s.feed(_hop_pcm(240))   # audio=480, gap=280 -> hold
    assert out == []
    out = s.feed(_hop_pcm(240))    # audio=720, gap=520 >= 400 -> emit
    assert [seg.text for seg in out] == ["hello"]


# ---------------------------------------------------------------------------
# Safety caps
# ---------------------------------------------------------------------------
def test_word_count_cap_emits_long_runon():
    # MAX_SENTENCE_WORDS words with no boundary -> cap forces emit.
    words = [_S(f"w{i}", i * 10, i * 10 + 8) for i in range(MAX_SENTENCE_WORDS)]
    s = SentenceCoalescer(FakeStreamer([words]))
    out = s.feed(_hop_pcm(240))
    assert len(out) == 1
    assert out[0].text.split() and len(out[0].text.split()) >= MAX_SENTENCE_WORDS


def test_time_span_cap_emits_long_sentence():
    # Words spaced < pause_ms apart (so no pause boundary fires) but
    # collectively spanning >= MAX_SENTENCE_MS (10 s) -> time-span cap.
    words = [_S(f"w{i}", i * 400, i * 400 + 200) for i in range(26)]
    # Consecutive gap = 400 - 200 = 200 ms (< 400 pause), so the only
    # boundary available is the cap. Span = 25*400 = 10000 ms.
    s = SentenceCoalescer(FakeStreamer([words]), pause_ms=400)
    out = s.feed(_hop_pcm(240))
    assert len(out) == 1
    assert out[0].text.split()[0] == "w0"
    assert out[0].text.split()[-1] == "w25"


# ---------------------------------------------------------------------------
# Multiple sentences in sequence
# ---------------------------------------------------------------------------
def test_multiple_sentences_split_on_punctuation():
    s = SentenceCoalescer(FakeStreamer([
        [_S("Hello", 0, 200)],
        [_S("world.", 200, 400)],
        [_S("How", 440, 600)],
        [_S("are", 600, 760)],
        [_S("you.", 760, 900)],
    ]))
    out = []
    for _ in range(5):
        out.extend(s.feed(_hop_pcm(240)))
    # The trailing-silence boundary may also fire on the last held words,
    # but we expect at least two distinct sentences ending in ".".
    texts = " ".join(seg.text for seg in out)
    assert "Hello world." in texts
    assert "How are you." in texts


# ---------------------------------------------------------------------------
# Empty / silence
# ---------------------------------------------------------------------------
def test_silence_only_emits_nothing():
    s = SentenceCoalescer(FakeStreamer([None, None, None]))
    assert s.feed(_hop_pcm(240)) == []
    assert s.feed(_hop_pcm(240)) == []
    assert s.feed(_hop_pcm(240)) == []
    assert s.flush() == []


def test_empty_pcm_does_not_advance_clock_or_emit():
    s = SentenceCoalescer(FakeStreamer([[_S("hi", 0, 100)]]))
    assert s.feed(b"") == []
    # Pending held; clock unchanged.
    assert s._audio_ms == 0
    assert s._pending  # the word was absorbed


# ---------------------------------------------------------------------------
# flush emits pending + delegate tail
# ---------------------------------------------------------------------------
def test_flush_emits_pending_and_streamer_tail():
    streamer = FakeStreamer(
        [[_S("hello", 0, 200)], None],
        flush_script=[_S("world", 240, 440)],
    )
    s = SentenceCoalescer(streamer, pause_ms=400)
    s.feed(_hop_pcm(240))  # hello held
    s.feed(_hop_pcm(240))  # silent, gap=280 < 400, still held
    tail = s.flush()
    # "hello" (pending) + "world" (streamer flush tail) -> one sentence.
    assert [seg.text for seg in tail] == ["hello world"]


def test_flush_empty_when_nothing_pending():
    s = SentenceCoalescer(FakeStreamer([None]))
    s.feed(_hop_pcm(240))
    assert s.flush() == []


# ---------------------------------------------------------------------------
# Streamer stand-in surface
# ---------------------------------------------------------------------------
def test_exposes_hop_ms_and_committed_ms_from_inner_streamer():
    streamer = FakeStreamer([[_S("hi", 0, 100)]], hop_ms=300)
    s = SentenceCoalescer(streamer)
    assert s._hop_ms == 300
    assert s.committed_ms == 0


# ---------------------------------------------------------------------------
# Pause threshold tuning
# ---------------------------------------------------------------------------
def test_custom_pause_threshold():
    # With a 100ms threshold, the 140ms trailing gap on the first hop
    # (audio=240, word.end=100) already clears it, so "hi" emits at once.
    s = SentenceCoalescer(
        FakeStreamer([[_S("hi", 0, 100)], None]), pause_ms=100,
    )
    out1 = s.feed(_hop_pcm(240))
    assert [seg.text for seg in out1] == ["hi"]


def test_400ms_threshold_holds_a_short_gap():
    # A 400 ms threshold (the previous default): 140 ms gap does NOT emit,
    # 380 ms still held, 620 ms emits. The default is now 1000 ms (see
    # test_default_pause_is_1000ms); this exercises the hold/emit logic at
    # an explicit 400 ms.
    s = SentenceCoalescer(
        FakeStreamer([[_S("hi", 0, 100)], None]), pause_ms=400,
    )
    out1 = s.feed(_hop_pcm(240))
    assert out1 == []
    out2 = s.feed(_hop_pcm(240))  # audio=480, gap=380 < 400 -> still held
    assert out2 == []
    out3 = s.feed(_hop_pcm(240))  # audio=720, gap=620 >= 400 -> emit
    assert [seg.text for seg in out3] == ["hi"]


def test_default_pause_is_1000ms():
    assert DEFAULT_PAUSE_MS == 1000


def test_default_pause_1000ms_holds_midthought_trailing_silence():
    # With the 1000 ms default, a word followed by silent hops must hold
    # until the trailing silence reaches 1000 ms (not 400 ms). hop=240 ms,
    # word "hello" ends at 200 ms; gap = audio_ms - 200.
    s = SentenceCoalescer(FakeStreamer([
        [_S("hello", 0, 200)],
        None,  # hop2: audio=480, gap=280 -> hold
        None,  # hop3: audio=720, gap=520 -> hold
        None,  # hop4: audio=960, gap=760 -> hold
        None,  # hop5: audio=1200, gap=1000 -> emit
    ]))
    assert s.feed(_hop_pcm(240)) == []   # hop1: audio=240, gap=40 -> hold
    assert s.feed(_hop_pcm(240)) == []   # hop2: gap=280 -> hold
    assert s.feed(_hop_pcm(240)) == []   # hop3: gap=520 -> hold
    out = s.feed(_hop_pcm(240))           # hop4: gap=760 (< 1000) -> hold
    assert out == []
    out = s.feed(_hop_pcm(240))           # hop5: gap=1000 (>= 1000) -> emit
    assert [seg.text for seg in out] == ["hello"]


# ---------------------------------------------------------------------------
# sentence_id is call-local and must be IGNORED for boundaries
#
# Both production backends stamp sentence_id with the segment's index within
# one transcribe() call (seg_idx + 1). The streamer calls the backend once
# per hop on a rolling window, so the id restarts every hop and carries no
# meaning across hops. An earlier revision treated id changes as model
# boundaries and suppressed punctuation while ids were "stable" — producing
# 40-word run-ons (id stuck at 1) and random mid-sentence splits (window
# segment count shifted). These tests pin the corrected behavior.
# ---------------------------------------------------------------------------
def test_id_change_alone_does_not_split():
    """An id flip with no punctuation and a sub-pause gap must NOT split:
    the id is call-local churn, not a model boundary."""
    streamer = FakeStreamer([
        [_Ss("hello", 0, 200, 1)],
        [_Ss("world", 240, 440, 2)],   # id 1 -> 2: call-local renumbering
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    assert s.feed(_hop_pcm(240)) == []
    assert s.feed(_hop_pcm(240)) == []
    tail = s.flush()
    assert [seg.text for seg in tail] == ["hello world"]


def test_punctuation_closes_sentence_even_with_nonzero_ids():
    """The run-on bug: with a constant id (> 0) the old coalescer suppressed
    punctuation, so "world." never closed the sentence. Punctuation must
    fire regardless of ids."""
    streamer = FakeStreamer([
        [_Ss("Hello", 0, 200, 1)],
        [_Ss("world.", 240, 440, 1)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    assert s.feed(_hop_pcm(240)) == []
    out2 = s.feed(_hop_pcm(240))
    assert [seg.text for seg in out2] == ["Hello world."]


def test_pause_boundary_fires_even_with_nonzero_ids():
    """The pause heuristic must not be gated on sentence_id == 0."""
    streamer = FakeStreamer([
        [_Ss("hello", 0, 200, 1)],
        [_Ss("again", 1400, 1600, 1)],   # 1200 ms gap >= 1000 ms pause
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    assert s.feed(_hop_pcm(240)) == []
    out2 = s.feed(_hop_pcm(240))
    assert [seg.text for seg in out2] == ["hello"]
    assert [seg.text for seg in s.flush()] == ["again"]


def test_same_id_accumulates_across_hops():
    """Unpunctuated words with sub-pause gaps accumulate into one sentence,
    emitted on flush — ids present or not."""
    streamer = FakeStreamer([
        [_Ss("dr", 0, 200, 1)],
        [_Ss("smith", 240, 440, 1)],
        [_Ss("went", 480, 680, 1)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    assert s.feed(_hop_pcm(240)) == []
    assert s.feed(_hop_pcm(240)) == []
    assert s.feed(_hop_pcm(240)) == []
    tail = s.flush()
    assert len(tail) == 1
    assert tail[0].text.split() == ["dr", "smith", "went"]


def test_abbreviation_period_does_not_split():
    """"Dr." ends with a period but is a mid-sentence abbreviation; the
    sentence must stay whole and close on "home."."""
    streamer = FakeStreamer([
        [_Ss("Dr.", 0, 200, 1)],
        [_Ss("Smith", 240, 440, 1)],
        [_Ss("went", 480, 680, 1)],
        [_Ss("home.", 720, 920, 1)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    out = []
    for _ in range(4):
        out.extend(s.feed(_hop_pcm(240)))
    tail = s.flush()
    sentences = [seg.text for seg in out] + [seg.text for seg in tail]
    assert sentences == ["Dr. Smith went home."]


def test_single_initial_period_does_not_split():
    assert not _ends_sentence("J.")
    assert not _ends_sentence("e.g.")
    assert _ends_sentence("home.")


def test_zero_id_punctuation_unchanged():
    """The original no-structure path: a period-bearing id-0 segment closes
    the sentence."""
    streamer = FakeStreamer([
        [_S("hello", 0, 200)],
        [_S("world.", 240, 440)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    assert s.feed(_hop_pcm(240)) == []
    out2 = s.feed(_hop_pcm(240))
    assert [seg.text for seg in out2] == ["hello world."]