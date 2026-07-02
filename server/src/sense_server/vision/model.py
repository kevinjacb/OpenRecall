"""OpenAI-compatible vision captioner — the provider-agnostic VisionModel.

Captions an image via the multimodal chat format (a user message carrying a text
prompt and an image as a base64 data URL). Any vision model that speaks the
OpenAI-compatible API works — local Qwen2.5-VL (mlx_vlm / Ollama) or a cloud model —
chosen by SENSE_VLM_* config. No vision model is hardcoded. Wire shape and config
are pure and unit-tested; only :meth:`caption` does I/O (lazy ``httpx``).
"""

from __future__ import annotations

import base64
from typing import Mapping, Protocol, runtime_checkable

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_PROMPT = (
    "Describe what is visible in this image in one concise sentence, focusing on "
    "details worth remembering: any visible text, people, objects, and the place."
)


@runtime_checkable
class VisionModel(Protocol):
    def caption(self, image: bytes, *, media_type: str = "image/jpeg") -> str:
        """Return a short description of the image."""
        ...


class OpenAICompatibleVisionModel:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        prompt: str = DEFAULT_PROMPT,
        temperature: float = 0.2,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.prompt = prompt
        self.temperature = temperature
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "OpenAICompatibleVisionModel":
        model = env.get("SENSE_VLM_MODEL")
        if not model:
            raise ValueError("SENSE_VLM_MODEL is required (the vision model name)")
        return cls(
            base_url=env.get("SENSE_VLM_BASE_URL", DEFAULT_BASE_URL),
            model=model,
            api_key=env.get("SENSE_VLM_API_KEY"),
        )

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _data_url(image: bytes, media_type: str) -> str:
        return f"data:{media_type};base64,{base64.b64encode(image).decode('ascii')}"

    def _payload(self, image: bytes, *, media_type: str, prompt: str) -> dict:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": self._data_url(image, media_type)},
                        },
                    ],
                }
            ],
        }

    def caption(self, image: bytes, *, media_type: str = "image/jpeg") -> str:
        import httpx

        resp = httpx.post(
            self._endpoint(),
            headers=self._headers(),
            json=self._payload(image, media_type=media_type, prompt=self.prompt),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
