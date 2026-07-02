"""OpenAI-compatible chat client — the provider-agnostic ChatModel.

One concrete :class:`~sense_server.memory.extract.ChatModel` that talks to *any*
OpenAI-compatible ``/chat/completions`` endpoint. That covers local models served by
Ollama or ``mlx_lm.server`` and cloud models (MiniMax, OpenAI, …) alike — the model
is chosen by ``(base_url, model, api_key)`` config, never hardcoded.

Wire-shape and config are pure and unit-tested; only :meth:`complete` does I/O and
lazily imports ``httpx`` (the ``llm`` extra).
"""

from __future__ import annotations

from typing import Mapping

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint


class OpenAICompatibleChatModel:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "OpenAICompatibleChatModel":
        """Build from environment: SENSE_LLM_{BASE_URL,MODEL,API_KEY,TEMPERATURE}."""
        model = env.get("SENSE_LLM_MODEL")
        if not model:
            raise ValueError("SENSE_LLM_MODEL is required (the model name to use)")
        return cls(
            base_url=env.get("SENSE_LLM_BASE_URL", DEFAULT_BASE_URL),
            model=model,
            api_key=env.get("SENSE_LLM_API_KEY"),
            temperature=float(env.get("SENSE_LLM_TEMPERATURE", "0.2")),
        )

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, system: str, user: str) -> dict:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

    def complete(self, system: str, user: str) -> str:
        import httpx

        resp = httpx.post(
            self._endpoint(),
            headers=self._headers(),
            json=self._payload(system, user),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
