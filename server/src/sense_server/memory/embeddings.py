"""OpenAI-compatible embedder — the provider-agnostic Embedder.

Mirrors the chat client: one concrete embedder that talks to any OpenAI-compatible
``/embeddings`` endpoint, local (Ollama, mlx) or cloud, chosen by SENSE_EMBED_*
config. No embedding model is hardcoded. Wire shape, env config, and response
parsing are pure and unit-tested; only :meth:`embed` does I/O (lazy ``httpx``).
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint

Vector = list[float]


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[Vector]:
        """Embed a batch of texts, returning one vector per input, in order."""
        ...


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "OpenAICompatibleEmbedder":
        model = env.get("SENSE_EMBED_MODEL")
        if not model:
            raise ValueError("SENSE_EMBED_MODEL is required (the embedding model name)")
        return cls(
            base_url=env.get("SENSE_EMBED_BASE_URL", DEFAULT_BASE_URL),
            model=model,
            api_key=env.get("SENSE_EMBED_API_KEY"),
        )

    def _endpoint(self) -> str:
        return f"{self.base_url}/embeddings"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, texts: list[str]) -> dict:
        return {"model": self.model, "input": texts}

    @staticmethod
    def _parse(body: dict, n: int) -> list[Vector]:
        """Restore embeddings into input order (responses carry an ``index``)."""
        vectors: list[Vector | None] = [None] * n
        for item in body["data"]:
            vectors[item["index"]] = item["embedding"]
        if any(v is None for v in vectors):
            raise ValueError("embedding response missing entries for some inputs")
        return [v for v in vectors if v is not None]

    def embed(self, texts: list[str]) -> list[Vector]:
        import httpx

        resp = httpx.post(
            self._endpoint(),
            headers=self._headers(),
            json=self._payload(texts),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse(resp.json(), len(texts))
