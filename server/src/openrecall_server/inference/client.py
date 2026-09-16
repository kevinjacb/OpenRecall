"""Synchronous HTTP clients implementing the in-process inference Protocols.

The Protocols are synchronous and run on a worker thread, so these use stdlib
urllib rather than an async client — and so the server gains no dependency.

Any transport or service failure surfaces as InferenceUnavailable. The caller
treats that like a backend that produced nothing: capture continues, the
transcript for that window is lost. Dropping audio would be worse.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..ingest.speaker_embedder import SpeakerVector
from ..ingest.streaming_transcriber import Token
from . import wire

DEFAULT_TIMEOUT_S = 30.0


class InferenceUnavailable(Exception):
    """The inference service could not be reached, or refused the request."""


def _post(base_url: str, path: str, payload: dict, timeout_s: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError) as exc:
        raise InferenceUnavailable(f"{path} failed: {exc}") from exc


def _get(base_url: str, path: str, timeout_s: float) -> dict:
    req = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError) as exc:
        raise InferenceUnavailable(f"{path} failed: {exc}") from exc


class HttpStreamingBackend:
    """Implements ingest.streaming_transcriber.StreamingBackend over HTTP."""

    def __init__(self, base_url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Token]:
        out = _post(self._base_url, "/transcribe",
                    {"pcm": wire.encode_pcm(pcm), "sample_rate": sample_rate},
                    self._timeout_s)
        return [wire.token_from_json(t) for t in out.get("tokens", [])]


class HttpSpeakerEmbedder:
    """Implements ingest.speaker_embedder.SpeakerEmbedder over HTTP.

    `dim` is fetched once from /info and cached: the identifier reads it per
    session, and it cannot change while the service is up.
    """

    def __init__(self, base_url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._dim: int | None = None

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        out = _post(self._base_url, "/embed",
                    {"pcm": wire.encode_pcm(pcm), "sample_rate": sample_rate},
                    self._timeout_s)
        v = out.get("vector")
        return None if v is None else [float(x) for x in v]

    @property
    def dim(self) -> int:
        if self._dim is None:
            reported = _get(self._base_url, "/info", self._timeout_s).get("embed_dim")
            # Refuse anything that is not a usable dimension rather than
            # coercing. This value is cached for the process lifetime and
            # speaker_identifier._mint writes it into every newly minted
            # Speaker row, so a bad one is durable corruption, not a bad
            # response. (The service answers 503 when its embedder is
            # unusable, which _get already turns into InferenceUnavailable;
            # this covers the rest.)
            if not isinstance(reported, int) or isinstance(reported, bool) \
                    or reported <= 0:
                raise InferenceUnavailable(
                    f"/info reported an unusable embed_dim: {reported!r}")
            self._dim = reported
        return self._dim

    def warmup(self) -> None:
        """Readiness probe.

        A service whose models failed to load answers /info with 503, which
        ``_get`` surfaces as InferenceUnavailable — so this is a real probe,
        not just a reachability check.
        """
        _get(self._base_url, "/info", self._timeout_s)
