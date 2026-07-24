"""Tests for the model-agnostic memory extractor.

LLMExtractor turns transcript text into structured memories by asking a *pluggable*
ChatModel (any OpenAI-compatible endpoint — local Gemma/Qwen via Ollama or
mlx_lm.server, or a cloud model like MiniMax/OpenAI). The model is injected, so
these tests use a fake and never touch a network or a real model.

Parse contract (strict, see issue "atoms never form"):
- A valid JSON ``[]`` is the only successful no-memory result.
- Anything else — non-JSON, JSON that is not an array, an array whose items
  are not the contracted ``{"kind","text"}`` shape (including the LLM's
  positional ``["event","text"]`` strings that some chat-tuned models emit) —
  must raise :class:`LLMParseError`. The caller (ExtractionWorker) is then
  responsible for leaving the cursor unchanged, counting the failure, and
  re-raising so the next enqueue / reconciliation retries.

The extractor must not silently swallow a malformed completion and return
``[]`` — the previous behavior caused the live "0 atoms" incident because
the LLM's positional-form output was being treated as "nothing to remember."
"""

import pytest

from sense_server.memory.extract import (
    ExtractedMemory,
    LLMExtractor,
    LLMExtractError,
    LLMParseError,
)


class FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


# --- prompt regression guard -------------------------------------------------


def test_default_prompt_pins_object_schema_against_positional_arrays():
    """The prompt must instruct the model to emit {kind,text} OBJECTS, not
    positional 2-string arrays. qwen2.5:7b emits ["fact","..."] without this
    guidance, which the strict parser rejects (extraction_parse_failed) and
    the cursor sticks (the 459-events-behind storm). This pins the fix against
    a regression that silently re-weakens the prompt.
    """
    from sense_server.memory.extract import DEFAULT_PROMPT

    assert '"kind"' in DEFAULT_PROMPT
    assert '"text"' in DEFAULT_PROMPT
    assert "flat array of strings" in DEFAULT_PROMPT
    # An explicit worked example anchors the shape for small chat-tuned models.
    assert '[{"kind"' in DEFAULT_PROMPT


# --- happy paths -------------------------------------------------------------


def test_parses_a_clean_json_array():
    chat = FakeChat('[{"kind": "fact", "text": "Kevin prefers tea"}]')

    out = LLMExtractor(chat).extract("...")

    assert out == [ExtractedMemory(kind="fact", text="Kevin prefers tea")]


def test_parses_json_wrapped_in_a_code_fence_with_prose():
    chat = FakeChat('Sure! Here you go:\n```json\n[{"kind":"task","text":"call Bob"}]\n```')

    out = LLMExtractor(chat).extract("x")

    assert out == [ExtractedMemory(kind="task", text="call Bob")]


def test_empty_array_is_the_only_successful_no_memory_result():
    """A valid ``[]`` is a success: no atoms, no exception, no counter.

    The contract is: only an explicit empty array means "the LLM looked at
    this transcript and found nothing memorable." Anything else is a
    parse failure that must surface.
    """
    assert LLMExtractor(FakeChat("[]")).extract("x") == []


def test_transcript_is_sent_as_the_user_message():
    chat = FakeChat("[]")

    LLMExtractor(chat).extract("hello world")

    assert chat.calls[0][1] == "hello world"
    assert chat.calls[0][0]  # a non-empty system prompt was supplied


# --- strict parse failure paths ---------------------------------------------


def test_non_json_output_raises_parse_error():
    """A prose reply (no JSON array) is a parse failure, not a no-memory.

    The previous behavior silently returned ``[]`` here, which is the
    exact bug that caused the live 0-atom incident when the LLM emitted
    free-form text instead of a JSON array.
    """
    with pytest.raises(LLMParseError):
        LLMExtractor(FakeChat("I couldn't find anything notable.")).extract("x")


def test_json_but_not_array_raises_parse_error():
    """A single JSON object (not an array) is a parse failure."""
    with pytest.raises(LLMParseError):
        LLMExtractor(FakeChat('{"kind":"fact","text":"hi"}')).extract("x")


def test_positional_array_form_raises_parse_error():
    """qwen2.5 and other chat-tuned models emit ``["event","text"]``
    positional 2-strings instead of the contracted ``{"kind","text"}``
    objects. The strict parser must reject this shape rather than
    silently return ``[]``."""
    with pytest.raises(LLMParseError):
        LLMExtractor(FakeChat('[["event", "Went to the gym this morning."]]')).extract("x")


def test_array_with_any_malformed_item_raises_parse_error():
    """If every item in the array is malformed, the reply is a parse
    failure. The previous behavior silently dropped bad items and
    returned the good ones — that is a footgun: a model that flips
    format mid-session will look like an empty extraction, advancing
    the cursor past memory that never got extracted."""
    reply = '[{"text":"no kind"}, "junk", 42, null, ["event", "x"]]'
    with pytest.raises(LLMParseError):
        LLMExtractor(FakeChat(reply)).extract("x")


def test_array_with_mixed_valid_and_malformed_items_raises_parse_error():
    """Even a single valid item does not 'rescue' an otherwise malformed
    array. The strict contract is: the LLM must produce the contracted
    shape for every item, or the whole reply is a parse failure.
    Splitting valid from invalid items is a parser-level leak — the
    upstream pipeline has no way to know which items came from which
    transcript, and the cursor-advance-vs-no-advance decision becomes
    ambiguous.
    """
    reply = '[{"kind":"fact","text":"ok"}, ["event","positional"]]'
    with pytest.raises(LLMParseError):
        LLMExtractor(FakeChat(reply)).extract("y")


def test_malformed_json_raises_parse_error():
    with pytest.raises(LLMParseError):
        LLMExtractor(FakeChat('[{')).extract("x")


def test_parse_error_is_a_subclass_of_llm_extract_error():
    """Callers that want to catch the whole family (parse + upstream)
    should be able to."""
    assert issubclass(LLMParseError, LLMExtractError)
