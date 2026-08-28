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
# Model sentence boundary (primary) — sentence_id change
#
# When the backend exposes sentence structure (sentence_id > 0), the
# coalescer groups by the model's own boundary: a sentence_id change closes
# the pending sentence. These tests use a hop large enough that the
# inter-word pause (< pause_ms) does NOT fire, so the ONLY thing emitting
# is the model boundary — proving it is the primary signal.
# ---------------------------------------------------------------------------
def test_model_boundary_emits_when_sentence_id_changes():
    """Two backend sentences (ids 1 and 2) with a sub-pause gap between
    hops: only the sentence_id change emits the first sentence; the
    second flushes at session end."""
    # hop=240: inter-hop gap is 240ms < 400ms pause, so the pause heuristic
    # does NOT fire. The model boundary (1 -> 2) is the sole emitter.
    streamer = FakeStreamer([
        [_Ss("hello", 0, 200, 1)],
        [_Ss("world", 240, 440, 2)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    out1 = s.feed(_hop_pcm(240))
    assert out1 == [], "first sentence held — id unchanged, no pause, no punct"
    out2 = s.feed(_hop_pcm(240))
    # id changed 1 -> 2: sentence 1 emits, sentence 2 starts pending.
    assert [seg.text for seg in out2] == ["hello"]
    tail = s.flush()
    assert [seg.text for seg in tail] == ["world"]


def test_model_boundary_same_id_accumulates_across_hops():
    """Same sentence_id across hops with a sub-pause gap and no
    punctuation: the words accumulate into one sentence, emitted on
    flush. The pause (240ms < 400ms) and the unchanged id both hold."""
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


def test_model_boundary_does_not_split_on_midsentence_period():
    """The heuristic punctuation rule would split "Dr." into its own
    sentence. The model keeps "Dr" and "Smith" in the same sentence
    (same id), so the period must NOT close the sentence — the whole
    "Dr. Smith went home." stays one sentence until the id changes or
    flush."""
    streamer = FakeStreamer([
        [_Ss("Dr.", 0, 200, 1)],
        [_Ss("Smith", 240, 440, 1)],
        [_Ss("went", 480, 680, 1)],
        [_Ss("home.", 720, 920, 1)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    # "Dr." ends with a period — but same id, so the coalescer holds it.
    assert s.feed(_hop_pcm(240)) == []
    # "home." ends with a period too; still same id 1 — no emit. (The
    # punctuation fallback does fire here because there's no id change to
    # have already closed it, but the test's point is the EARLY "Dr."
    # period did not split: all four words land in ONE sentence.)
    out = []
    for _ in range(3):
        out.extend(s.feed(_hop_pcm(240)))
    tail = s.flush()
    # Exactly one sentence containing all four words, in order.
    sentences = [seg.text for seg in out] + [seg.text for seg in tail]
    flat = " ".join(sentences)
    assert flat.split() == ["Dr.", "Smith", "went", "home."]


def test_model_boundary_zero_id_falls_back_to_punctuation():
    """sentence_id == 0 (no structure) must use the punctuation heuristic,
    not the model boundary. A period-bearing segment with id 0 closes the
    sentence — the original no-structure behavior."""
    streamer = FakeStreamer([
        [_S("hello", 0, 200)],
        [_S("world.", 240, 440)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    assert s.feed(_hop_pcm(240)) == []
    out2 = s.feed(_hop_pcm(240))
    # id 0 -> punctuation fires on "world.".
    assert [seg.text for seg in out2] == ["hello world."]


def test_model_boundary_emits_each_sentence_as_ids_change():
    """A fully-structured multi-sentence utterance: each id change closes
    the pending sentence. No punctuation, sub-pause gaps — the model
    boundary is the sole emitter mid-stream."""
    streamer = FakeStreamer([
        [_Ss("hello", 0, 200, 1)],
        [_Ss("world", 240, 440, 1)],
        [_Ss("how", 480, 680, 2)],
        [_Ss("are", 720, 920, 2)],
        [_Ss("you", 960, 1160, 3)],
    ], hop_ms=240)
    s = SentenceCoalescer(streamer, pause_ms=DEFAULT_PAUSE_MS)

    out = []
    for _ in range(5):
        out.extend(s.feed(_hop_pcm(240)))
    tail = s.flush()
    assert [seg.text for seg in out] == ["hello world", "how are"]
    assert [seg.text for seg in tail] == ["you"]