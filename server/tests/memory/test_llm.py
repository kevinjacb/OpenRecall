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


# --- unreachable endpoint -----------------------------------------------------

def test_a_refused_connection_explains_both_causes(monkeypatch):
    """A bare "[Errno 111] Connection refused" under 40 lines of httpx says
    where the call died and nothing about why. Observed on a real deployment
    2026-09-20, where the LLM was running but bound to loopback."""
    import httpx

    from openrecall_server.memory.llm import OpenAICompatibleChatModel

    def refuse(*a, **k):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(httpx, "post", refuse)
    model = OpenAICompatibleChatModel(
        model="m", base_url="http://host.docker.internal:11434/v1")

    with pytest.raises(httpx.ConnectError) as excinfo:
        model.complete("sys", "user")
    message = str(excinfo.value)
    assert "host.docker.internal:11434" in message, "name the endpoint tried"
    assert "curl" in message, "give a command that checks cause 1"
    assert "capture, transcription and audio are unaffected" in message, (
        "say what still works — this failure looks total from the traceback")


def test_the_container_hint_names_the_loopback_trap(monkeypatch):
    """Inside a container the subtle cause is a server that IS running but
    bound to 127.0.0.1 — the same trap as the inference sidecar's
    --host 0.0.0.0, and indistinguishable from 'not running' at this layer."""
    import httpx

    from openrecall_server.memory import llm as llm_module

    monkeypatch.setattr(llm_module.os.path, "exists", lambda p: p == "/.dockerenv")
    message = str(llm_module._unreachable_hint(
        "http://host.docker.internal:11434/v1",
        httpx.ConnectError("refused")))
    assert "OLLAMA_HOST=0.0.0.0" in message
    assert "host.docker.internal" in message


def test_outside_a_container_the_advice_stays_short(monkeypatch):
    """Loopback binding is not the likely cause on a bare host, so do not
    send someone chasing a container problem they do not have."""
    import httpx

    from openrecall_server.memory import llm as llm_module

    monkeypatch.setattr(llm_module.os.path, "exists", lambda p: False)
    message = str(llm_module._unreachable_hint(
        "http://localhost:11434/v1", httpx.ConnectError("refused")))
    assert "OLLAMA_HOST=0.0.0.0" not in message
