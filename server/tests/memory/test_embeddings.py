"""Tests for the model-agnostic embedder's wire shape, config, and response parsing.

Like the chat model, the embedder talks to any OpenAI-compatible ``/embeddings``
endpoint (local Ollama/mlx, or cloud), chosen by SENSE_EMBED_* config. The HTTP send
is a thin shell; the pure parts — request shape, env config, and parsing the batched
response back into per-input vectors in order — are tested here.
"""

import pytest

from sense_server.memory.embeddings import OpenAICompatibleEmbedder


def test_payload_carries_model_and_batched_input():
    e = OpenAICompatibleEmbedder(base_url="http://x/v1", model="nomic-embed")
    payload = e._payload(["a", "b"])
    assert payload == {"model": "nomic-embed", "input": ["a", "b"]}


def test_endpoint_and_auth_header():
    e = OpenAICompatibleEmbedder(base_url="http://x/v1/", model="m", api_key="sk-9")
    assert e._endpoint() == "http://x/v1/embeddings"
    assert e._headers()["Authorization"] == "Bearer sk-9"


def test_parse_returns_vectors_in_input_order():
    # OpenAI returns objects with an "index"; we must restore input order.
    body = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    assert OpenAICompatibleEmbedder._parse(body, n=2) == [[1.0, 0.0], [0.0, 1.0]]


def test_from_env_requires_a_model():
    with pytest.raises(ValueError):
        OpenAICompatibleEmbedder.from_env({"SENSE_EMBED_BASE_URL": "http://x/v1"})


def test_from_env_reads_config():
    e = OpenAICompatibleEmbedder.from_env(
        {
            "SENSE_EMBED_BASE_URL": "http://localhost:11434/v1",
            "SENSE_EMBED_MODEL": "nomic-embed-text",
            "SENSE_EMBED_API_KEY": "k",
        }
    )
    assert e.model == "nomic-embed-text"
    assert e._endpoint() == "http://localhost:11434/v1/embeddings"
    assert e._headers()["Authorization"] == "Bearer k"
