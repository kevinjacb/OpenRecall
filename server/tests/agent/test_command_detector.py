import asyncio
from types import SimpleNamespace

from openrecall_server.agent.command_detector import CommandDetector, match_command_phrase
from openrecall_server.agent.config import CommandDetectorConfig, DEFAULT_COMMAND_PHRASES

_P = DEFAULT_COMMAND_PHRASES


def test_match_returns_command_type_on_phrase_present():
    assert match_command_phrase("hey take a photo of mine", _P) == "capture_photo"


def test_match_longest_phrase_wins():
    # "take a photo" must beat "take a" if a shorter prefix were ever a key.
    p = {"take a": "x", "take a photo": "capture_photo"}
    assert match_command_phrase("take a photo", p) == "capture_photo"


def test_match_handles_split_phrase_across_rolling_buffer():
    # Imprecise coalescing can split "take a" ... "photo" across sentences;
    # the rolling buffer joins them, so the phrase still matches.
    assert match_command_phrase("so I said take a and then photo please", _P) is None
    # But when the buffer actually contains the contiguous phrase:
    assert match_command_phrase("earlier talk. take a photo now", _P) == "capture_photo"


def test_match_case_insensitive():
    assert match_command_phrase("TAKE A PHOTO", _P) == "capture_photo"


def test_match_bare_common_word_is_not_a_trigger():
    # "stop" alone is not a key, so normal speech mentioning "stop" misses.
    assert match_command_phrase("she stopped the car abruptly", _P) is None
    assert match_command_phrase("I started to think about it", _P) is None


def test_match_returns_none_when_no_phrase():
    assert match_command_phrase("hello there how are you", _P) is None
    assert match_command_phrase("anything", {}) is None


# --- Task 3: CommandDetector.feed -------------------------------------------

class FakeSpeakerRegistry:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, speaker_id):
        return self._m.get(speaker_id)


class FakeLoop:
    """Records call_soon_threadsafe callbacks AND invokes them, so the
    monkey-patched ``_launch_stage2`` lambda actually populates ``_launched``.
    Tests that expect no scheduling assert ``loop.calls == []`` (feed returns
    before ever calling this)."""
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, fn, *args):
        self.calls.append((fn, args))
        fn(*args)


def _event(text, speaker=None, assignment=None, event_id="s:0", start_ms=0, session_id="s"):
    return SimpleNamespace(
        session_id=session_id, event_id=event_id, seq=0, text=text,
        speaker=speaker, speaker_assignment=assignment, start_ms=start_ms,
    )


def _speaker(is_wearer):
    return SimpleNamespace(is_wearer=is_wearer, display_name="You")


def _detector(require_wearer=True, loop=None, phrases=None, stage2=None, cooldown=3.0, max_inflight=1):
    cfg = CommandDetectorConfig(
        enabled=True, require_wearer=require_wearer, cooldown_s=cooldown,
        max_inflight=max_inflight, phrases=phrases or dict(DEFAULT_COMMAND_PHRASES),
    )
    d = CommandDetector(
        model=None, dispatcher=None, command_validator=None, capability_provider=None,
        speaker_registry=None, loop=loop or FakeLoop(), config=cfg, ids=None, clock=None,
    )
    # Stage 2 not implemented yet; record its launches.
    d._launched = []
    d._launch_stage2 = lambda sid, ev, ctype, ctx: d._launched.append((sid, ev, ctype, ctx))
    return d


def test_feed_confirmed_wearer_schedules_stage2():
    loop = FakeLoop()
    reg = FakeSpeakerRegistry({"spk-1": _speaker(is_wearer=True)})
    d = _detector(loop=loop)
    d._speaker_registry = reg
    d.feed("s", _event("take a photo", speaker="spk-1", assignment="confirmed"))
    assert len(loop.calls) == 1
    _sid, _ev, ctype, _ctx = d._launched[0]
    assert ctype == "capture_photo"


def test_feed_non_wearer_does_not_schedule():
    loop = FakeLoop()
    reg = FakeSpeakerRegistry({"spk-2": _speaker(is_wearer=False)})
    d = _detector(loop=loop)
    d._speaker_registry = reg
    d.feed("s", _event("take a photo", speaker="spk-2", assignment="confirmed"))
    assert loop.calls == []
    assert d._launched == []


def test_feed_tentative_or_none_speaker_does_not_schedule():
    loop = FakeLoop()
    d = _detector(loop=loop)
    d._speaker_registry = FakeSpeakerRegistry({"spk-1": _speaker(True)})
    d.feed("s", _event("take a photo", speaker="spk-1", assignment="tentative"))
    assert d._launched == []
    d.feed("s", _event("take a photo", speaker=None, assignment=None))
    assert d._launched == []


def test_feed_require_wearer_false_allows_unknown_speaker():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop)
    d._speaker_registry = None  # not required
    d.feed("s", _event("take a photo", speaker=None, assignment=None))
    assert len(d._launched) == 1


def test_feed_no_phrase_match_does_not_schedule():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop)
    d.feed("s", _event("hello there how are you"))
    assert d._launched == []


def test_feed_dedup_within_cooldown_drops_second():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop, cooldown=100.0)
    d.feed("s", _event("take a photo"))
    d.feed("s", _event("take a photo"))
    assert len(d._launched) == 1  # second dropped by cooldown


def test_feed_split_phrase_matches_across_buffer():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop)
    d.feed("s", _event("so I said take a"))
    d.feed("s", _event("photo please"))
    assert len(d._launched) == 1


def test_feed_inflight_cap_drops_when_full():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop, max_inflight=1, cooldown=100.0)
    d.feed("s", _event("take a photo", event_id="s:1"))
    # Different command type but in-flight is full -> dropped.
    d.feed("s", _event("start a video", event_id="s:2"))
    assert len(d._launched) == 1