import asyncio
from types import SimpleNamespace

import pytest

from openrecall_server.agent.command_detector import (
    CommandDetector,
    match_command_phrase,
    parse_stage2_reply,
    stage2_messages,
)
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


# --- Task 4: Stage 2 prompt builder + reply parser --------------------------

_ALLOWED = ("capture_photo", "start_video", "stop_video", "start_audio",
            "stop_audio", "record_video", "flush_snapshots")


def test_stage2_messages_lists_types_and_carries_context():
    system, user = stage2_messages("take a photo of mine", _ALLOWED)
    assert "capture_photo" in system
    assert "narration" in system.lower()
    assert "take a photo of mine" in user


def test_parse_valid_command():
    r = parse_stage2_reply('{"command":{"type":"capture_photo","params":{}},"confidence":0.9}', _ALLOWED)
    assert r.command_type == "capture_photo"
    assert r.params == {}
    assert r.confidence == 0.9


def test_parse_null_command():
    r = parse_stage2_reply('{"command":null,"confidence":0.1}', _ALLOWED)
    assert r.command_type is None
    assert r.confidence == 0.1


def test_parse_rejects_invalid_type():
    r = parse_stage2_reply('{"command":{"type":"delete_everything","params":{}},"confidence":0.99}', _ALLOWED)
    assert r.command_type is None  # not in the allowed vocabulary


def test_parse_rejects_malformed_json():
    r = parse_stage2_reply("not json", _ALLOWED)
    assert r.command_type is None
    assert r.confidence == 0.0


def test_parse_rejects_missing_fields():
    r = parse_stage2_reply('{"command":{"type":"capture_photo"}}', _ALLOWED)
    assert r.command_type is None  # missing confidence -> not a valid command


def test_parse_clamps_confidence():
    r = parse_stage2_reply('{"command":{"type":"capture_photo","params":{}},"confidence":1.5}', _ALLOWED)
    assert r.command_type is None  # confidence out of [0,1] -> reject


# --- Task 5: Stage 2 LLM confirmation + dispatch ----------------------------

from openrecall_server.agent.command_detector import CommandDetector, derive_idempotency_key
from openrecall_server.agent.config import CommandDetectorConfig, DEFAULT_COMMAND_PHRASES
from openrecall_server.contracts.clock import FakeClock


class FakeChatModel:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self._reply


class FakeValidator:
    def __init__(self, reject=False):
        self._reject = reject

    def validate(self, payload):
        from openrecall_server.agent.validator_command import (
            CommandValidationResult, ValidatedCommand)
        if self._reject:
            return CommandValidationResult(command=None, rejection="unknown", message="no")
        return CommandValidationResult(
            command=ValidatedCommand(
                command_type=payload.command_type, params=payload.params,
                idempotency_key=payload.idempotency_key, confidence=payload.confidence),
            rejection=None, message=None)


class FakeCaps:
    """Parametrizable capability/resource provider for guardrail tests.

    Defaults advertise every capability and ample resources, so the
    guardrail-refusal branch is only exercised when a test opts in
    (``camera=False`` or ``battery`` below the quick-op floor).
    """

    def __init__(self, *, camera=True, battery=0.9):
        self._camera = camera
        self._battery = battery

    def capabilities(self):
        from openrecall_server.contracts.types import CapabilitySet
        return CapabilitySet(camera=self._camera, microphone=True,
                             retrospective_buffer=True)

    def resources(self):
        from openrecall_server.contracts.types import DeviceResourceStatus
        return DeviceResourceStatus(battery_pct=self._battery,
                                    storage_free_bytes=1 << 30,
                                    recording=False, relay_connected=True)


class FakeDispatcher:
    def __init__(self):
        self.issued = []

    def issue(self, command):
        self.issued.append(command)
        from openrecall_server.commands.signing import SignedCommand
        return SignedCommand(command=command, payload=command.canonical_bytes(), signature=b"")


def _stage2_detector(reply, reject=False, caps=None):
    cfg = CommandDetectorConfig(enabled=True, require_wearer=False, confidence_threshold=0.8,
                                phrases=dict(DEFAULT_COMMAND_PHRASES))
    loop = FakeLoop()

    class _FakeIds:
        def new(self):
            return "cmd-1"

    d = CommandDetector(
        model=FakeChatModel(reply), dispatcher=FakeDispatcher(), command_validator=FakeValidator(reject),
        capability_provider=caps or FakeCaps(), speaker_registry=None, loop=loop, config=cfg,
        ids=_FakeIds(), clock=FakeClock(),
    )
    return d, loop


async def test_stage2_confirmed_command_dispatches():
    d, _ = _stage2_detector('{"command":{"type":"capture_photo","params":{}},"confidence":0.9}')
    ev = _event("take a photo", event_id="s:7")
    await d._run_stage2("s", ev, "capture_photo", "take a photo")
    assert len(d._dispatcher.issued) == 1
    assert d._dispatcher.issued[0].type == "capture_photo"
    assert d._provenance["cmd-1"] == "s:7"
    assert d._inflight.get("s", 0) == 0  # decremented after completion


async def test_stage2_null_command_does_not_dispatch():
    d, _ = _stage2_detector('{"command":null,"confidence":0.1}')
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []


async def test_stage2_low_confidence_does_not_dispatch():
    d, _ = _stage2_detector('{"command":{"type":"capture_photo","params":{}},"confidence":0.5}')
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []


async def test_stage2_validator_rejection_does_not_dispatch():
    d, _ = _stage2_detector('{"command":{"type":"capture_photo","params":{}},"confidence":0.9}', reject=True)
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []


async def test_stage2_guardrail_no_camera_does_not_dispatch():
    """Spec-mandated: guardrail refusal (no camera) does not dispatch.

    Validation passes (FakeValidator reject=False) so the real
    StrictCommandGuardrails constructed inside _dispatch is what refuses
    — exercising the real refusal branch, not a stub.
    """
    d, _ = _stage2_detector(
        '{"command":{"type":"capture_photo","params":{}},"confidence":0.9}',
        caps=FakeCaps(camera=False),
    )
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []  # guardrail refused → no dispatch
    assert d._inflight.get("s", 0) == 0  # in-flight still decremented


async def test_stage2_guardrail_low_battery_does_not_dispatch():
    """Battery below the quick-op floor (5%) refuses capture_photo."""
    d, _ = _stage2_detector(
        '{"command":{"type":"capture_photo","params":{}},"confidence":0.9}',
        caps=FakeCaps(battery=0.04),
    )
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []
    assert d._inflight.get("s", 0) == 0  # in-flight decremented even on refusal


def test_derive_idempotency_key_is_stable_and_typespecific():
    k1 = derive_idempotency_key("s", "capture_photo", "take a photo of mine")
    k2 = derive_idempotency_key("s", "capture_photo", "take a photo of mine")
    k3 = derive_idempotency_key("s", "start_video", "take a photo of mine")
    assert k1 == k2
    assert k1 != k3