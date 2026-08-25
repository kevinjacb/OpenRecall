from openrecall_server.agent.command_detector import match_command_phrase
from openrecall_server.agent.config import DEFAULT_COMMAND_PHRASES

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