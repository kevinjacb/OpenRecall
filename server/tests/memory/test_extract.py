"""Tests for the model-agnostic memory extractor.

LLMExtractor turns transcript text into structured memories by asking a *pluggable*
ChatModel (any OpenAI-compatible endpoint — local Gemma/Qwen via Ollama or
mlx_lm.server, or a cloud model like MiniMax/OpenAI). The model is injected, so
these tests use a fake and never touch a network or a real model.

The robustness focus is parsing the model's reply: real models wrap JSON in code
fences, add prose, or emit malformed items. The extractor must be tolerant and never
crash the ingest path on a bad completion.
"""

from sense_server.memory.extract import ExtractedMemory, LLMExtractor


class FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


def test_parses_a_clean_json_array():
    chat = FakeChat('[{"kind": "fact", "text": "Kevin prefers tea"}]')

    out = LLMExtractor(chat).extract("...")

    assert out == [ExtractedMemory(kind="fact", text="Kevin prefers tea")]


def test_parses_json_wrapped_in_a_code_fence_with_prose():
    chat = FakeChat('Sure! Here you go:\n```json\n[{"kind":"task","text":"call Bob"}]\n```')

    out = LLMExtractor(chat).extract("x")

    assert out == [ExtractedMemory(kind="task", text="call Bob")]


def test_empty_array_yields_no_memories():
    assert LLMExtractor(FakeChat("[]")).extract("x") == []


def test_non_json_output_is_handled_gracefully():
    assert LLMExtractor(FakeChat("I couldn't find anything notable.")).extract("x") == []


def test_malformed_items_are_skipped_not_fatal():
    chat = FakeChat(
        '[{"kind":"fact","text":"ok"}, {"text":"no kind"}, "junk", {"kind":"task","text":"go"}]'
    )

    out = LLMExtractor(chat).extract("y")

    assert out == [
        ExtractedMemory(kind="fact", text="ok"),
        ExtractedMemory(kind="task", text="go"),
    ]


def test_transcript_is_sent_as_the_user_message():
    chat = FakeChat("[]")

    LLMExtractor(chat).extract("hello world")

    assert chat.calls[0][1] == "hello world"
    assert chat.calls[0][0]  # a non-empty system prompt was supplied
