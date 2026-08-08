"""Tests for the OpenAI-compatible chat client's wire shape and configuration.

The actual HTTP send is a thin shell (needs a live endpoint); what we lock here is
the request contract every OpenAI-compatible backend expects, and that the model is
fully configurable — model name, base URL, and optional auth all come from config /
env, so pointing at local Gemma/Qwen (Ollama, mlx_lm.server) or a cloud model
(MiniMax/OpenAI) is configuration only.
"""

import pytest

from openrecall_server.memory.llm import OpenAICompatibleChatModel


def test_payload_carries_model_and_chat_messages():
    m = OpenAICompatibleChatModel(base_url="http://x/v1", model="gemma2", temperature=0.1)

    payload = m._payload("SYS", "USER")

    assert payload["model"] == "gemma2"
    assert payload["temperature"] == 0.1
    assert payload["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]


def test_auth_header_present_only_when_api_key_is_set():
    with_key = OpenAICompatibleChatModel(base_url="http://x/v1", model="m", api_key="sk-1")
    without = OpenAICompatibleChatModel(base_url="http://x/v1", model="m")

    assert with_key._headers()["Authorization"] == "Bearer sk-1"
    assert "Authorization" not in without._headers()


def test_base_url_trailing_slash_is_normalised():
    m = OpenAICompatibleChatModel(base_url="http://x/v1/", model="m")
    assert m._endpoint() == "http://x/v1/chat/completions"


def test_from_env_reads_model_base_url_and_key():
    env = {
        "OPENRECALL_LLM_BASE_URL": "http://localhost:11434/v1",
        "OPENRECALL_LLM_MODEL": "gemma2",
        "OPENRECALL_LLM_API_KEY": "secret",
    }

    m = OpenAICompatibleChatModel.from_env(env)

    assert m.model == "gemma2"
    assert m._endpoint() == "http://localhost:11434/v1/chat/completions"
    assert m._headers()["Authorization"] == "Bearer secret"


def test_from_env_requires_a_model():
    with pytest.raises(ValueError):
        OpenAICompatibleChatModel.from_env({"OPENRECALL_LLM_BASE_URL": "http://x/v1"})
