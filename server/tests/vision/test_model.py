"""Tests for the model-agnostic vision captioner's wire shape and config.

Captioning uses the OpenAI-compatible multimodal chat format (a user message with a
text part and an image_url data-URL part), so any vision model works — local
(Qwen2.5-VL via mlx_vlm/Ollama) or cloud — chosen by OPENSAPIEN_VLM_* config. The HTTP
send is the only untested shell.
"""

import base64

import pytest

from opensapien_server.vision.model import OpenAICompatibleVisionModel


def test_payload_has_text_and_image_url_parts():
    m = OpenAICompatibleVisionModel(base_url="http://x/v1", model="qwen2.5-vl")
    img = b"\xff\xd8\xff\xe0jpeg"

    payload = m._payload(img, media_type="image/jpeg", prompt="What is this?")

    assert payload["model"] == "qwen2.5-vl"
    (message,) = payload["messages"]
    assert message["role"] == "user"
    text_part, image_part = message["content"]
    assert text_part == {"type": "text", "text": "What is this?"}
    assert image_part["type"] == "image_url"
    expected_url = "data:image/jpeg;base64," + base64.b64encode(img).decode("ascii")
    assert image_part["image_url"]["url"] == expected_url


def test_endpoint_and_auth_header():
    m = OpenAICompatibleVisionModel(base_url="http://x/v1/", model="m", api_key="sk-7")
    assert m._endpoint() == "http://x/v1/chat/completions"
    assert m._headers()["Authorization"] == "Bearer sk-7"


def test_from_env_requires_a_model():
    with pytest.raises(ValueError):
        OpenAICompatibleVisionModel.from_env({"OPENSAPIEN_VLM_BASE_URL": "http://x/v1"})


def test_from_env_reads_config():
    m = OpenAICompatibleVisionModel.from_env(
        {
            "OPENSAPIEN_VLM_BASE_URL": "http://localhost:8080/v1",
            "OPENSAPIEN_VLM_MODEL": "qwen2.5-vl",
            "OPENSAPIEN_VLM_API_KEY": "k",
        }
    )
    assert m.model == "qwen2.5-vl"
    assert m._endpoint() == "http://localhost:8080/v1/chat/completions"
    assert m._headers()["Authorization"] == "Bearer k"
