"""OpenAI-compatible chat client — the provider-agnostic ChatModel.

One concrete :class:`~openrecall_server.memory.extract.ChatModel` that talks to *any*
OpenAI-compatible ``/chat/completions`` endpoint. That covers local models served by
Ollama or ``mlx_lm.server`` and cloud models (MiniMax, OpenAI, …) alike — the model
is chosen by ``(base_url, model, api_key)`` config, never hardcoded.

Wire-shape and config are pure and unit-tested; only :meth:`complete` does I/O and
lazily imports ``httpx`` (the ``llm`` extra).
"""

from __future__ import annotations

import os
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
        """Build from environment: OPENRECALL_LLM_{BASE_URL,MODEL,API_KEY,TEMPERATURE}."""
        model = env.get("OPENRECALL_LLM_MODEL")
        if not model:
            raise ValueError("OPENRECALL_LLM_MODEL is required (the model name to use)")
        return cls(
            base_url=env.get("OPENRECALL_LLM_BASE_URL", DEFAULT_BASE_URL),
            model=model,
            api_key=env.get("OPENRECALL_LLM_API_KEY"),
            temperature=float(env.get("OPENRECALL_LLM_TEMPERATURE", "0.2")),
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

        try:
            resp = httpx.post(
                self._endpoint(),
                headers=self._headers(),
                json=self._payload(system, user),
                timeout=self.timeout,
            )
        except httpx.ConnectError as exc:
            raise _unreachable_hint(self.base_url, exc) from exc
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _unreachable_hint(base_url: str, exc: Exception) -> Exception:
    """Explain a refused connection to the LLM endpoint.

    This surfaces as a bare "[Errno 111] Connection refused" under a 40-line
    httpx traceback, which says where the call died and nothing about why.
    There are only two real causes and the advice differs, so name both.

    The subtle one is a server that IS running: Ollama binds 127.0.0.1 by
    default, which is unreachable from inside a container even when
    host.docker.internal resolves correctly. It is the same trap as the
    inference sidecar's --host 0.0.0.0, and it looks identical to "not
    running" from here.
    """
    import httpx

    in_container = os.path.exists("/.dockerenv")
    lines = [
        f"cannot reach the LLM at {base_url} ({exc})",
        "",
        "Either nothing is listening there, or it is listening only on "
        "loopback. Check in this order:",
        f"  1. the service is up:   curl {base_url}/models",
    ]
    if in_container:
        lines += [
            "  2. it accepts connections from OUTSIDE the host's loopback.",
            "     Ollama binds 127.0.0.1 by default, which a container cannot",
            "     reach even when host.docker.internal resolves. Start it with",
            "     OLLAMA_HOST=0.0.0.0:11434 — the same reason the inference",
            "     sidecar needs --host 0.0.0.0.",
            "  3. this process points at the host, not at itself:",
            "     OPENRECALL_LLM_BASE_URL=http://host.docker.internal:11434/v1",
        ]
    else:
        lines += [
            "  2. OPENRECALL_LLM_BASE_URL points where the service actually is.",
        ]
    lines += [
        "",
        "Until then memory extraction and segment titles produce nothing; "
        "capture, transcription and audio are unaffected.",
    ]
    return httpx.ConnectError("\n".join(lines))
