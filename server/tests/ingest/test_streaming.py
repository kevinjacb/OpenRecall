"""Tests for the streaming transcriber (no-boundary-loss transcript path).

The current AudioIngestPipeline cuts audio into 5s windows and feeds each
to Whisper independently. Speech that straddles a window boundary
(stress-test: a word that starts at 4.95s and finishes at 5.10s) gets
split: the tail is in window N+1 with no prior context, the head is
in window N with no following context. Whisper hallucinates or
re-transcribes, producing "the the the" or "and- and- and-" patterns.

The fix is streaming: feed 1s hops with 5s of context, use Whisper's
token-level timestamps to commit only text confirmed by an overlapping
window, and emit the committed text in stable segments.
"""
from __future__ import annotations

import pytest

from openrecall_server.ingest.streaming_transcriber import (
    Segment,
    streaming_from_tokens,
    Token,
)


# --- fakes ------------------------------------------------------------------


class FakeWhisper:
    """A deterministic fake that returns predictable token-level timestamps.

    Each call to ``transcribe`` returns one token whose ``start`` is the
    absolute offset into the audio passed in. The test's job is to
    verify the streaming wrapper correctly commits tokens once they're
    confirmed by an overlapping window — the fake encodes that by
    returning the same tokens when re-transcribing overlapping audio.
    """

    def __init__(self, scripted: list[list[Token]]) -> None:
        """``scripted[i]`` is what the i-th ``transcribe`` call returns."""
        self._scripted = list(scripted)
        self.calls: int = 0

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        if self.calls >= len(self._scripted):
            return []
        out = self._scripted[self.calls]
        self.calls += 1
        return out


def _tok(text: str, start: float, end: float) -> Token:
    return Token(text=text, start_ms=int(start * 1000), end_ms=int(end * 1000))


def _toks(text: str, start: float, end: float, sid: int) -> Token:
    """A token tagged with a real backend sentence id (> 0)."""
    return Token(text=text, start_ms=int(start * 1000), end_ms=int(end * 1000),
                 sentence_id=sid)


# --- tests ------------------------------------------------------------------


def test_first_hop_emits_committed_prefix():
    """The first hop's tokens are immediately committed (no overlap yet)."""
    whisper = FakeWhisper([
        [_tok("hello", 0.0, 0.4), _tok("world", 0.4, 0.8)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    segments = s.feed(b"\x00" * 16000 * 1)  # 1s of audio
    assert len(segments) == 1
    assert segments[0].text == "hello world"
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 800


def test_hop_boundary_does_not_duplicate():
    """Word spanning the hop boundary must appear in exactly one segment."""
    # First hop: "hello world" (0-1s).
    # Second hop: window is 0.5s-5.5s. The fake says "world" is at 0.5-0.9
    # (in the overlap zone, retried by Whisper; the retried text matches
    # what we already committed, so the streaming wrapper must not
    # emit it again).
    # Third hop (just for completeness): empty.
    whisper = FakeWhisper([
        [_tok("hello", 0.0, 0.4), _tok("world", 0.4, 0.8)],
        [_tok("world", 0.5, 0.9), _tok("foo", 1.0, 1.4)],
        [],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    seg1 = s.feed(b"\x00" * 16000 * 1)
    seg2 = s.feed(b"\x00" * 16000 * 1)
    seg3 = s.feed(b"\x00" * 16000 * 1)
    assert seg1[0].text == "hello world"
    # The second hop's "world" was already in the first segment; only "foo" is new.
    assert "".join(seg.text for seg in seg2) == "foo"
    assert seg3 == []


def test_hop_emits_new_text_only():
    """A hop's new content is the tokens whose start is past the committed prefix."""
    whisper = FakeWhisper([
        [_tok("alpha", 0.0, 0.4)],
        [_tok("alpha", 0.0, 0.4), _tok("beta", 0.5, 0.9), _tok("gamma", 1.0, 1.4)],
        [],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    seg1 = s.feed(b"\x00" * 16000 * 1)
    seg2 = s.feed(b"\x00" * 16000 * 1)
    assert seg1[0].text == "alpha"
    assert "".join(seg.text for seg in seg2) == "beta gamma"


def test_overlap_zone_new_text_is_emitted_via_text_merge():
    """If a hop's overlap retrial returns DIFFERENT text in the overlap zone
    (a word whose retranscribed start jittered behind the cursor), the
    text-merge emits the new word — it's a real word the previous hop missed
    (L7 fix). Already-committed text is still dropped. The wrapper does NOT
    retroactively change the prefix; the new word is appended.
    """
    whisper = FakeWhisper([
        [_tok("hello", 0.0, 0.4), _tok("world", 0.4, 0.8)],
        [_tok("different", 0.5, 1.0), _tok("foo", 1.0, 1.4)],
        [],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    seg1 = s.feed(b"\x00" * 16000 * 1)
    seg2 = s.feed(b"\x00" * 16000 * 1)
    # First segment keeps "hello world" — the wrapper does not retroactively
    # change the prefix based on a divergent overlap retrial.
    assert seg1[0].text == "hello world"
    # "different" straddles the cursor (start=500 < committed=800, end=1000 > 800)
    # and its text is NOT in the last segment ("hello world") → emitted (L7 fix).
    # "foo" is past the committed prefix so it's emitted.
    assert "".join(seg.text for seg in seg2) == "different foo"


def test_token_behind_cursor_with_new_text_is_emitted():
    """L7 fix: a word whose retranscribed start_ms jittered behind the cursor
    but whose text was NOT in the last emitted segment IS emitted (it's a real
    word the previous hop missed), NOT hard-dropped. Already-committed words
    are NOT re-emitted.
    """
    whisper = FakeWhisper([
        [_tok("hello", 0.0, 0.4), _tok("world", 0.4, 0.8)],  # committed=800
        # "missed" straddles the cursor (start=350 < 800, end=900 > 800)
        [_tok("missed", 0.35, 0.9), _tok("foo", 1.0, 1.4)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    seg1 = s.feed(b"\x00" * 16000)
    seg2 = s.feed(b"\x00" * 16000)
    assert seg1[0].text == "hello world"
    # "missed" is new text (not in {"hello", "world"}) → emitted despite
    # its start being behind the cursor.
    assert "missed" in seg2[0].text
    assert "foo" in seg2[0].text
    # Already-committed words are NOT re-emitted.
    assert "hello" not in seg2[0].text
    assert "world" not in seg2[0].text


def test_genuine_repeated_word_emitted_twice():
    """A genuinely repeated word (said twice, both past the cursor) MUST be
    emitted twice — the dedup is timestamp/position-aware, not pure text-equality.
    The cross-hop dedup only fires for multi-word phrases (>= _DEDUP_MIN_WORDS),
    so a single word repeating is not collapsed.
    """
    whisper = FakeWhisper([
        [_tok("hello", 0.0, 0.4)],          # emit; committed=400
        [_tok("hello", 0.5, 0.9)],          # past cursor, genuine repeat → emit
        [_tok("hello", 1.5, 1.9)],          # past cursor, genuine repeat → emit
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    seg1 = s.feed(b"\x00" * 16000)
    seg2 = s.feed(b"\x00" * 16000)
    seg3 = s.feed(b"\x00" * 16000)
    assert seg1[0].text == "hello"
    assert seg2[0].text == "hello"   # not cross-hop deduped (single word)
    assert seg3[0].text == "hello"   # not cross-hop deduped


def test_short_partial_window_at_session_end():
    """The final hop may be shorter than the full hop; tokens still commit."""
    whisper = FakeWhisper([
        [_tok("first", 0.0, 0.4)],
        [],
        [_tok("last", 0.0, 0.4)],  # 0.5s of audio, last at 0-0.4s
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    s.feed(b"\x00" * 16000 * 1)
    s.feed(b"\x00" * 16000 * 1)
    # The "last" token is at 0-0.4s in the 0.5s clip. After 2 hops,
    # committed_ms == 400, so the test verifies that hop 3 doesn't
    # re-emit. The segment from hop 1 is the only thing emitted.
    segments = s.feed(b"\x00" * 8000)  # 0.5s
    assert segments == []
    # But calling flush() re-runs the backend and emits any
    # uncommitted tail. The third hop returned a token at 0-0.4s
    # which is entirely in the committed zone, so flush emits nothing
    # either. The bound is the test of the streaming invariant:
    # nothing leaks past the committed cursor.
    assert s.flush() == []


def test_empty_hop_emits_nothing():
    whisper = FakeWhisper([[], [], []])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    for _ in range(3):
        assert s.feed(b"\x00" * 16000) == []


def test_segments_carry_absolute_timestamps():
    """Segment end_ms is the absolute time in the session, not the hop-local time.

    Until the rolling window's trim kicks in, hop-local coordinates
    are the same as absolute coordinates (the buffer hasn't been
    trimmed yet). The test exercises the simplest case.
    """
    whisper = FakeWhisper([
        [_tok("a", 0.0, 0.2)],
        [_tok("a", 0.0, 0.2), _tok("b", 0.5, 0.7)],
        [],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    s.feed(b"\x00" * 16000)  # hop 0: emits "a" at absolute 0-200ms
    seg2 = s.feed(b"\x00" * 16000)  # hop 1: 1s elapsed; "b" at hop-local 0.5-0.7
    # Buffer hasn't been trimmed yet (2 hops = 2s < 5s window), so
    # _buffer_start_ms is still 0. The token's absolute start = 0 + 500 = 500ms.
    assert seg2[0].text == "b"
    assert seg2[0].start_ms == 500
    assert seg2[0].end_ms == 700


def test_streaming_transcriber_rejects_invalid_config():
    with pytest.raises(ValueError):
        streaming_from_tokens(FakeWhisper([]), sample_rate=16000, hop_ms=600, window_ms=500)
    with pytest.raises(ValueError):
        streaming_from_tokens(FakeWhisper([]), sample_rate=16000, hop_ms=0, window_ms=2000)


def test_drops_overlap_beyond_window():
    """Tokens that are entirely in the overlap zone are already committed
    and must not be re-emitted.
    """
    whisper = FakeWhisper([
        [_tok("x", 0.0, 0.2)],
        [_tok("x", 0.0, 0.2), _tok("y", 4.0, 4.4)],  # y is way past the committed prefix
        [],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    s.feed(b"\x00" * 16000)
    seg2 = s.feed(b"\x00" * 16000)
    # "y" is in the new zone (1.0-2.0s absolute → hop-local 0.0-1.0s). Even though
    # the fake put it at hop-local 4.0, the wrapper trusts Whisper's
    # timestamps and emits it. The test is really asserting that "x" (in
    # the overlap zone) is NOT re-emitted.
    text = "".join(seg.text for seg in seg2)
    assert "x" not in text
    assert "y" in text


# --- cross-hop short-phrase dedup (Fix D) ------------------------------------
# Whisper hallucinating the same short phrase on consecutive noise hops
# produces a fresh token each hop at a NEW absolute time (past the committed
# cursor), so the overlap-zone dedup does NOT catch it — the transcript fills
# with "Thank you. Thank you. Thank you." The cross-hop dedup collapses a
# short segment identical to the last EMITTED one.


def test_cross_hop_identical_short_phrase_is_deduped():
    """Three consecutive identical short phantoms collapse to one emit."""
    whisper = FakeWhisper([
        [_tok("thank you", 0.0, 0.5)],      # emit; committed_ms -> 500
        [_tok("thank you", 0.6, 1.0)],      # new (600 >= 500) but dup -> drop
        [_tok("thank you", 0.6, 1.0)],      # still dup of last emitted -> drop
        [_tok("real speech here", 0.6, 1.0)],  # different -> emit
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    seg1 = s.feed(b"\x00" * 16000)
    seg2 = s.feed(b"\x00" * 16000)
    seg3 = s.feed(b"\x00" * 16000)
    seg4 = s.feed(b"\x00" * 16000)
    assert seg1[0].text == "thank you"
    assert seg2 == []   # deduped
    assert seg3 == []   # deduped (still matches last EMITTED "thank you")
    assert seg4[0].text == "real speech here"


def test_cross_hop_dedup_does_not_advance_committed_cursor():
    """Dropping a dup must NOT advance the committed cursor, so a later real
    token that lands inside the phantom's span is still emitted."""
    whisper = FakeWhisper([
        [_tok("thank you", 0.0, 0.5)],      # emit; committed_ms = 500
        [_tok("thank you", 0.6, 1.0)],      # dup -> drop (cursor stays 500)
        [_tok("okay", 0.55, 0.7)],          # 550 >= 500 -> new, different -> emit
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    s.feed(b"\x00" * 16000)
    s.feed(b"\x00" * 16000)
    seg3 = s.feed(b"\x00" * 16000)
    assert seg3[0].text == "okay"


def test_cross_hop_dedup_skips_long_phrases():
    """A long identical phrase repeated across hops is NOT collapsed — long
    repeats are rare and real, and the repetition filter handles true loops."""
    long_text = "this is a longer phrase that should not be deduped here at all"
    whisper = FakeWhisper([
        [_tok(long_text, 0.0, 0.5)],        # emit; committed_ms = 500
        [_tok(long_text, 0.6, 1.0)],        # 12 words > _DEDUP_MAX_WORDS -> emit
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    seg1 = s.feed(b"\x00" * 16000)
    seg2 = s.feed(b"\x00" * 16000)
    assert seg1[0].text == long_text
    assert seg2[0].text == long_text   # not deduped


def test_cross_hop_dedup_normalizes_punctuation():
    """Different trailing punctuation across hops still counts as a dup."""
    whisper = FakeWhisper([
        [_tok("thank you.", 0.0, 0.5)],
        [_tok("thank you!", 0.6, 1.0)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    s.feed(b"\x00" * 16000)
    seg2 = s.feed(b"\x00" * 16000)
    assert seg2 == []   # "thank you!" normalized == "thank you." -> deduped


# --- opt-in webrtcvad gate (Fix A) ------------------------------------------


def test_vad_disabled_calls_backend_on_silence():
    """Default (vad_mode=None): a silent hop still calls the backend (the
    blocklist/repetition filters handle phantoms, not a pre-gate)."""
    whisper = FakeWhisper([[_tok("thank you", 0.0, 0.5)], []])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    segs = s.feed(b"\x00" * 32000)  # 1s of silence
    assert whisper.calls == 1   # backend WAS called
    assert segs[0].text == "thank you"   # emitted (no pre-gate)


def test_webrtcvad_gate_skips_silent_hop():
    """With vad_mode='webrtc', a pure-silence hop is skipped before the
    backend call so Whisper never sees it and can't hallucinate on it.
    webrtcvad deterministically reports all-zero frames as non-speech."""
    pytest.importorskip("webrtcvad")
    whisper = FakeWhisper([[_tok("thank you", 0.0, 0.5)]])
    s = streaming_from_tokens(
        whisper, sample_rate=16000, hop_ms=1000, window_ms=5000, vad_mode="webrtc",
    )
    segs = s.feed(b"\x00" * 32000)  # 1s of silence
    assert segs == []
    assert whisper.calls == 0   # the backend was never called


def test_webrtcvad_invalid_aggressiveness_raises():
    pytest.importorskip("webrtcvad")
    with pytest.raises(ValueError, match="vad_aggressiveness"):
        streaming_from_tokens(
            FakeWhisper([]), sample_rate=16000, hop_ms=1000, window_ms=5000,
            vad_mode="webrtc", vad_aggressiveness=4,
        )


# --- sentence_id: per-sentence Segment emission ----------------------------


def test_tokens_with_different_sentence_ids_emit_one_segment_per_sentence():
    """When the backend tags tokens with sentence ids, a single hop whose
    new tokens span two backend sentences emits TWO Segments — one per
    sentence, each carrying its sentence_id. This is how the model's own
    segmentation is carried through the committed-cursor / dedup logic."""
    whisper = FakeWhisper([
        # First hop: one sentence "hello world" (id 1).
        [_toks("hello", 0.0, 0.4, 1), _toks("world", 0.4, 0.8, 1)],
        # Second hop: the new tokens span the end of sentence 1 ("foo")
        # and the start of sentence 2 ("bar baz") — two sentences in one hop.
        [_toks("world", 0.5, 0.9, 1), _toks("foo", 1.0, 1.4, 1),
         _toks("bar", 1.5, 1.9, 2), _toks("baz", 1.9, 2.3, 2)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)

    seg1 = s.feed(b"\x00" * 16000)
    assert len(seg1) == 1
    assert seg1[0].text == "hello world"
    assert seg1[0].sentence_id == 1

    seg2 = s.feed(b"\x00" * 16000)
    # "world" is behind the cursor (already committed); the new tokens are
    # "foo" (id 1) then "bar baz" (id 2) — two Segments, one per sentence.
    assert len(seg2) == 2
    assert seg2[0].text == "foo"
    assert seg2[0].sentence_id == 1
    assert seg2[1].text == "bar baz"
    assert seg2[1].sentence_id == 2


def test_tokens_with_zero_sentence_id_collapse_into_one_segment():
    """sentence_id == 0 (no structure: str adapter, or the flattened
    AlignedResult.tokens fallback) collapses all new tokens into a single
    Segment — preserving the original one-Segment-per-hop behavior."""
    whisper = FakeWhisper([
        [_tok("alpha", 0.0, 0.4), _tok("beta", 0.4, 0.8), _tok("gamma", 0.8, 1.2)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)

    segs = s.feed(b"\x00" * 16000)
    assert len(segs) == 1
    assert segs[0].text == "alpha beta gamma"
    assert segs[0].sentence_id == 0


def test_flush_emits_one_segment_per_pending_sentence_id():
    """flush() also groups by sentence_id, emitting one Segment per backend
    sentence among the final uncommitted tokens.

    Hop 1 commits "hello" (id 1) and advances the cursor to 400ms. flush()
    re-transcribes the buffer; its tokens (sentences 2 and 3) all start past
    the committed cursor, so they are emitted grouped by sentence_id.
    """
    whisper = FakeWhisper([
        [_toks("hello", 0.0, 0.4, 1)],                                   # feed
        [_toks("how", 0.5, 0.9, 2), _toks("are", 0.9, 1.3, 2),          # flush
         _toks("you", 1.4, 1.8, 3)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    s.feed(b"\x00" * 16000)            # commits "hello" (id 1); cursor=400
    tail = s.flush()                   # flush transcribes; ids 2 & 3 past cursor
    texts = [seg.text for seg in tail]
    ids = [seg.sentence_id for seg in tail]
    assert texts == ["how are", "you"]
    assert ids == [2, 3]


def test_leading_space_tokens_join_single_spaced_in_feed():
    """mlx-whisper prefixes non-first word tokens with a leading space; the
    streamer must join them to a single-spaced sentence, not 'Hello,  world!'."""
    whisper = FakeWhisper([
        [Token(text="Hello,", start_ms=0, end_ms=400),
         Token(text=" world!", start_ms=400, end_ms=800)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    segments = s.feed(b"\x00" * 16000)  # 1 s of audio
    assert len(segments) == 1
    assert segments[0].text == "Hello, world!"


def test_leading_space_tokens_join_single_spaced_in_flush():
    """The flush path (session-end drain) must strip leading spaces too."""
    # feed commits "Hello," (0-400); the flush call re-transcribes and adds
    # " world!" (400-800) past the committed cursor -> emitted via flush.
    whisper = FakeWhisper([
        [Token(text="Hello,", start_ms=0, end_ms=400)],
        [Token(text="Hello,", start_ms=0, end_ms=400),
         Token(text=" world!", start_ms=400, end_ms=800)],
    ])
    s = streaming_from_tokens(whisper, sample_rate=16000, hop_ms=1000, window_ms=5000)
    feed_segs = s.feed(b"\x00" * 16000)
    assert [seg.text for seg in feed_segs] == ["Hello,"]
    flush_segs = s.flush()
    assert [seg.text for seg in flush_segs] == ["world!"]
