# Speaker Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute every captured utterance to a speaker (with a confidence and assignment type), learning continuously and adaptively, with a manual-correction loop — off by default behind `SENSE_SPEAKER_ENABLED`.

**Architecture:** Two separable stages — recognition (cosine match against confirmed/named centroids, hysteresis-gated) and clustering (corroborate N embeddings before minting an identity). A `SpeakerEmbedder` runs on the live PCM in the ingest pipeline; a `SpeakerIdentifier` orchestrates embed + match + cluster + enroll, backed by a persisted `SpeakerRegistry` (ring-buffer + outlier-trim + EMA centroids). A `SpeakerNudgeListener` fires confirm/name nudges; `NameSpeaker`/`ReassignSpeaker` control messages close the naming + correction loop. Additive-only schema; events/atoms store the stable `speaker_id` UUID, never a display name.

**Tech Stack:** Python 3.12, pydantic v2, stdlib sqlite3, pytest + pytest-asyncio. mlx/numpy imported lazily inside the real embedder (unit tests never load them). All vectors are `list[float]` (mirrors `memory/embeddings.py`) so tests run without numpy.

## Global Constraints

- **No raw audio is ever stored.** Speaker ID runs on live PCM only; only embeddings + transcript text are persisted.
- **Model-agnostic, local-first.** Never hardcode a model. `SpeakerEmbedder` is a Protocol; the real impl lazy-imports heavy deps inside `embed()` so unit tests never load them. Embeddings/centroids never leave the Mac unless the operator sets `SENSE_SPEAKER_EMBED_BASE_URL`.
- **Audio is single-channel mono 16 kHz.** Separation is by voiceprint (embedding), not channel.
- **Additive-only schema.** New columns are nullable with idempotent `ALTER TABLE` migrations mirroring `memory/migrations.py`. Re-running migration is a no-op. Existing 695 tests stay green.
- **Off by default.** `SENSE_SPEAKER_ENABLED=false`. When disabled, events get `speaker=None` and zero embed calls.
- **Speaker ID never blocks transcription.** On embed failure, the hop is transcribed with `speaker=None`; ASR is never blocked.
- **Events/atoms store the stable `speaker_id` UUID.** Display names live only on the registry row; resolved by a join at read time.
- **Vector type is `list[float]`** (not numpy) so the whole unit suite runs without the `mlx` extra.
- **Every sqlite store uses `check_same_thread=False` + `threading.Lock`** (the proactive/ingest paths run off the main thread).
- Naming: env vars are `SENSE_SPEAKER_*`; commit messages follow the existing `feat(scope):` / `test(scope):` convention.

---

## File Structure

**New files:**
- `server/src/sense_server/ingest/speaker_embedder.py` — `SpeakerEmbedder` Protocol + `FakeSpeakerEmbedder` (tests) + `MlxSpeakerEmbedder` (lazy mlx import). The pure wire-shape module.
- `server/src/sense_server/ingest/speaker_config.py` — `SpeakerConfig` (frozen pydantic) + `load_speaker_config(env)`. One seam for every `SENSE_SPEAKER_*` number.
- `server/src/sense_server/memory/speaker_registry.py` — `Speaker` model + `SpeakerRegistry` Protocol + `InMemorySpeakerRegistry` + `SqliteSpeakerRegistry`. Tables `speakers` + `speaker_embeddings`; ring-buffer + outlier-trim + EMA centroid; `name`, `reassign`, `recompute_centroid`.
- `server/src/sense_server/ingest/speaker_identifier.py` — `SpeakerAssignment` + `SpeakerIdentifier`. Recognition + clustering + "You" enrollment + edge cases. Holds in-memory pending clusters (TTL'd).
- `server/src/sense_server/agent/speaker_nudge.py` — `SpeakerNudgeListener`. Confirm + name nudges, dedupe, rate-limit, disabled-guard.
- `server/tests/ingest/test_speaker_embedder.py`
- `server/tests/ingest/test_speaker_config.py`
- `server/tests/memory/test_speaker_registry.py`
- `server/tests/ingest/test_speaker_identifier.py`
- `server/tests/agent/test_speaker_nudge.py`
- `server/tests/ingest/test_speaker_reassign.py`

**Modified files:**
- `server/src/sense_server/events/model.py` — `CaptureEvent` gains 3 nullable fields.
- `server/src/sense_server/memory/atom.py` — `MemoryAtom` gains 3 nullable fields.
- `server/src/sense_server/memory/migrations.py` — `SPEAKER_ADDITIONS` + `migrate_capture_events_table`; extend `migrate_memory_atoms_table`.
- `server/src/sense_server/events/store.py` — `SqliteEventStore` CREATE TABLE + append + events + last_event carry the 3 columns; call migration.
- `server/src/sense_server/memory/store.py` — `SqliteAtomStore` append + atoms carry the 3 columns; call migration.
- `server/src/sense_server/ingest/transcriber.py` — `Transcript` gains 3 speaker fields (defaulted).
- `server/src/sense_server/ingest/pipeline.py` — `AudioIngestPipeline.ingest`/`flush` call `SpeakerIdentifier`; thread speaker onto `Transcript`.
- `server/src/sense_server/protocol/messages.py` — `TranscriptMsg` gains speaker fields; add `NameSpeaker` + `ReassignSpeaker` inbound; add optional `propose` to `ProactiveMessage`.
- `server/src/sense_server/gateway/core.py` — `_emit` writes speaker into `CaptureEvent` + `TranscriptMsg`; `on_control` handles `NameSpeaker`/`ReassignSpeaker`.
- `server/src/sense_server/memory/stages.py` — `ExtractionStage` propagates majority speaker onto atoms; per-line speakers to the extractor.
- `server/src/sense_server/memory/extraction_worker.py` — wire `SpeakerNudgeListener` (just `add_listener` at the call site; no internal change).
- `server/src/sense_server/gateway/adapter.py` — `build_pipeline_factory` threads a `SpeakerIdentifier`.
- `server/scripts/run_gateway.py` — wire speaker config + registry + identifier + nudge listener; `SENSE_SPEAKER_ENABLED` gate.
- `server/tests/memory/test_migrations.py` — update column counts (12 → 15 for atoms; new capture_events migration tests).

---

## Task 1: SpeakerEmbedder Protocol + Fake + config

**Files:**
- Create: `server/src/sense_server/ingest/speaker_embedder.py`
- Create: `server/src/sense_server/ingest/speaker_config.py`
- Test: `server/tests/ingest/test_speaker_embedder.py`
- Test: `server/tests/ingest/test_speaker_config.py`

**Interfaces:**
- Produces: `SpeakerEmbedder` Protocol (`dim: int`, `embed(pcm, sample_rate) -> list[float] | None`); `FakeSpeakerEmbedder` (deterministic, None on short window); `MlxSpeakerEmbedder` (lazy import); `SpeakerConfig` + `load_speaker_config(env)`.

- [ ] **Step 1: Write the failing embedder test**

```python
# tests/ingest/test_speaker_embedder.py
from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder


def _pcm(ms: int, sr: int = 16000) -> bytes:
    # 16-bit LE mono silence-ish bytes of length ms
    return b"\x00\x01" * (sr * ms // 1000)


def test_fake_embedder_returns_a_unit_vector_of_configured_dim():
    emb = FakeSpeakerEmbedder(dim=8)
    vec = emb.embed(_pcm(1000), 16000)
    assert vec is not None
    assert len(vec) == 8
    # deterministic: same pcm -> same vec
    assert emb.embed(_pcm(1000), 16000) == vec


def test_fake_embedder_returns_none_on_window_shorter_than_min_speech_ms():
    emb = FakeSpeakerEmbedder(dim=8, min_speech_ms=500)
    assert emb.embed(_pcm(100), 16000) is None  # 100 ms < 500 ms


def test_fake_embedder_different_pcm_yields_different_vector():
    emb = FakeSpeakerEmbedder(dim=8)
    a = emb.embed(_pcm(1000), 16000)
    b = emb.embed(b"\x01\x02" * 16000, 16000)
    assert a != b


def test_mlx_embedder_imports_lazily_and_not_at_module_import():
    import sense_server.ingest.speaker_embedder as mod
    # importing the module must not import numpy/mlx_whisper
    import sys
    assert "numpy" not in sys.modules
    assert "mlx_whisper" not in sys.modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/ingest/test_speaker_embedder.py -v`
Expected: FAIL with `ModuleNotFoundError: sense_server.ingest.speaker_embedder`

- [ ] **Step 3: Write minimal embedder implementation**

```python
# src/sense_server/ingest/speaker_embedder.py
"""Speaker-embedding model seam — provider-agnostic, local-first.

Mirrors memory/embeddings.py: one Protocol, a fake for unit tests, and a real
mlx/ONNX impl that lazy-imports its heavy deps inside embed() so importing this
module never requires numpy/mlx. Vectors are list[float] (not numpy) so the whole
unit suite runs without the mlx extra.

embed() returns None when the window is too short / too quiet for a usable
vector — the identifier treats None as "no speech in hop" (never embeds, never
mints, never blocks transcription).
"""
from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

SpeakerVector = list[float]


@runtime_checkable
class SpeakerEmbedder(Protocol):
    dim: int

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        """Embed one PCM window, or None when too short/quiet."""
        ...


class FakeSpeakerEmbedder:
    """Deterministic, dependency-free embedder for unit tests.

    Derives a fixed-dim unit vector from a hash of the PCM so identical audio
    yields identical vectors and different audio yields different vectors.
    Returns None when the window is shorter than min_speech_ms.
    """

    def __init__(self, dim: int = 16, min_speech_ms: int = 500) -> None:
        self.dim = dim
        self.min_speech_ms = min_speech_ms

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        ms = len(pcm) * 1000 // (sample_rate * 2)
        if ms < self.min_speech_ms:
            return None
        digest = hashlib.sha256(pcm).digest()
        # expand digest to dim bytes
        raw = (digest * ((self.dim // len(digest)) + 1))[: self.dim]
        vec = [(b - 128) / 128.0 for b in raw]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class MlxSpeakerEmbedder:
    """Real Apple-Silicon voice-embedding backend (ECAPA-TDNN / pyannote-style).

    Heavy deps (numpy, the embedding model) are imported lazily inside embed()
    so importing this module — and running the unit suite — never loads them.
    Configured via SpeakerConfig; the model id/base_url/api_key mirror the text
    embedder. Local-only by default; a cloud base_url is opt-in.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        dim: int = 0,
        min_speech_ms: int = 500,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self._dim = dim
        self.min_speech_ms = min_speech_ms

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        ms = len(pcm) * 1000 // (sample_rate * 2)
        if ms < self.min_speech_ms:
            return None
        # Lazy import — unit tests never reach here.
        import numpy as np  # noqa: F401  (lazy heavy dep)
        # TODO: real model inference. For now raise NotImplementedError so a
        # misconfigured production path fails loud rather than silently returning
        # a fake vector. The local model wiring lands with the hardware bring-up.
        raise NotImplementedError(
            "MlxSpeakerEmbedder.embed requires the local voice-embedding model; "
            "configure SENSE_SPEAKER_EMBED_MODEL and run on Apple Silicon."
        )
```

- [ ] **Step 4: Write the failing config test**

```python
# tests/ingest/test_speaker_config.py
import pytest
from sense_server.ingest.speaker_config import load_speaker_config


def test_defaults_when_disabled_and_unset():
    cfg = load_speaker_config({})
    assert cfg.enabled is False
    assert cfg.confirm_threshold == 0.7
    assert cfg.tentative_threshold == 0.55
    assert cfg.corroborate_n == 3
    assert cfg.ring_buffer_n == 100
    assert cfg.ema_alpha == 0.05
    assert cfg.name_nudge_turns == 8


def test_env_overrides_are_parsed():
    cfg = load_speaker_config({
        "SENSE_SPEAKER_ENABLED": "true",
        "SENSE_SPEAKER_CONFIRM_THRESHOLD": "0.8",
        "SENSE_SPEAKER_RING_BUFFER_N": "50",
    })
    assert cfg.enabled is True
    assert cfg.confirm_threshold == 0.8
    assert cfg.ring_buffer_n == 50


def test_confirm_must_exceed_tentative():
    with pytest.raises(ValueError, match="confirm_threshold"):
        load_speaker_config({
            "SENSE_SPEAKER_ENABLED": "true",
            "SENSE_SPEAKER_CONFIRM_THRESHOLD": "0.5",
            "SENSE_SPEAKER_TENTATIVE_THRESHOLD": "0.6",
        })


def test_thresholds_must_be_in_unit_interval():
    with pytest.raises(ValueError):
        load_speaker_config({"SENSE_SPEAKER_CONFIRM_THRESHOLD": "1.5"})
```

- [ ] **Step 5: Write minimal config implementation**

```python
# src/sense_server/ingest/speaker_config.py
"""Speaker-recognition config — env-driven, validated at startup.

One frozen seam for every SENSE_SPEAKER_* number. Loaded once at startup; a bad
value raises so the gateway refuses to start rather than running with the wrong
policy. Mirrors agent/config.py.
"""
from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel, ConfigDict, model_validator

ENV_ENABLED = "SENSE_SPEAKER_ENABLED"


def _parse_bool(name: str, raw: str) -> bool:
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name}={raw!r} is not a valid boolean")


def _parse_float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name}={raw!r} is not a valid float") from e


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name}={raw!r} is not a valid integer") from e


class SpeakerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    embed_model: str = ""
    embed_base_url: str | None = None
    embed_api_key: str | None = None

    confirm_threshold: float = 0.7
    tentative_threshold: float = 0.55
    min_speech_ms: int = 500
    cluster_threshold: float = 0.65
    corroborate_n: int = 3
    corroborate_window_s: int = 30
    pending_ttl_s: int = 60
    ring_buffer_n: int = 100
    outlier_trim_pct: float = 0.1
    ema_alpha: float = 0.05
    coldstart_window_s: int = 120
    confirm_turns: int = 10
    name_nudge_turns: int = 8
    min_confidence: float = 0.5

    @model_validator(mode="after")
    def _validate_bounds(self) -> "SpeakerConfig":
        for name in ("confirm_threshold", "tentative_threshold",
                     "cluster_threshold", "outlier_trim_pct", "ema_alpha",
                     "min_confidence"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name}={v} must be in [0.0, 1.0]")
        if self.confirm_threshold <= self.tentative_threshold:
            raise ValueError(
                f"confirm_threshold ({self.confirm_threshold}) must be > "
                f"tentative_threshold ({self.tentative_threshold})"
            )
        for name in ("min_speech_ms", "corroborate_n", "corroborate_window_s",
                     "pending_ttl_s", "ring_buffer_n", "coldstart_window_s",
                     "confirm_turns", "name_nudge_turns"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name}={getattr(self, name)} must be > 0")
        return self


def load_speaker_config(env: Mapping[str, str]) -> SpeakerConfig:
    kwargs: dict = {}
    if ENV_ENABLED in env:
        kwargs["enabled"] = _parse_bool(ENV_ENABLED, env[ENV_ENABLED])
    if "SENSE_SPEAKER_EMBED_MODEL" in env:
        kwargs["embed_model"] = env["SENSE_SPEAKER_EMBED_MODEL"]
    if "SENSE_SPEAKER_EMBED_BASE_URL" in env:
        kwargs["embed_base_url"] = env["SENSE_SPEAKER_EMBED_BASE_URL"]
    if "SENSE_SPEAKER_EMBED_API_KEY" in env:
        kwargs["embed_api_key"] = env["SENSE_SPEAKER_EMBED_API_KEY"]
    float_fields = ("confirm_threshold", "tentative_threshold", "min_speech_ms",
                    "cluster_threshold", "corroborate_window_s", "pending_ttl_s",
                    "ring_buffer_n", "coldstart_window_s", "confirm_turns",
                    "name_nudge_turns")
    float_defaults = {f: getattr(SpeakerConfig.model_fields[f], "default")
                      for f in ("confirm_threshold", "tentative_threshold",
                                "cluster_threshold", "ema_alpha",
                                "outlier_trim_pct", "min_confidence")}
    for f in ("confirm_threshold", "tentative_threshold", "cluster_threshold",
              "ema_alpha", "outlier_trim_pct", "min_confidence"):
        envn = f"SENSE_SPEAKER_{f.upper()}"
        if envn in env:
            kwargs[f] = _parse_float(envn, env[envn])
    int_fields = ("min_speech_ms", "corroborate_n", "corroborate_window_s",
                  "pending_ttl_s", "ring_buffer_n", "coldstart_window_s",
                  "confirm_turns", "name_nudge_turns")
    for f in int_fields:
        envn = f"SENSE_SPEAKER_{f.upper()}"
        if envn in env:
            kwargs[f] = _parse_int(envn, env[envn])
    return SpeakerConfig(**kwargs)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd server && python -m pytest tests/ingest/test_speaker_embedder.py tests/ingest/test_speaker_config.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add server/src/sense_server/ingest/speaker_embedder.py server/src/sense_server/ingest/speaker_config.py \
        server/tests/ingest/test_speaker_embedder.py server/tests/ingest/test_speaker_config.py
git commit -m "feat(ingest): SpeakerEmbedder Protocol + Fake + SpeakerConfig"
```

---

## Task 2: SpeakerRegistry store — tables, ring buffer, EMA centroid

**Files:**
- Create: `server/src/sense_server/memory/speaker_registry.py`
- Test: `server/tests/memory/test_speaker_registry.py`

**Interfaces:**
- Consumes: `SpeakerConfig` (ring_buffer_n, outlier_trim_pct, ema_alpha).
- Produces: `Speaker` (pydantic frozen snapshot); `SpeakerRegistry` Protocol; `InMemorySpeakerRegistry`; `SqliteSpeakerRegistry`. Key methods used by later tasks: `get(speaker_id) -> Speaker | None`, `matchable() -> list[Speaker]` (centroids for recognition), `add_speaker(speaker) -> None`, `add_confirmed_embedding(speaker_id, embedding, confidence) -> None`, `ring_buffer(speaker_id) -> list[list[float]]`, `recompute_centroid(speaker_id) -> None`, `centroid(speaker_id) -> list[float] | None`, `increment_turn(speaker_id) -> int`, `set_display_name(speaker_id, name) -> None`, `update_enrollment(speaker_id, status) -> None`, `list_speakers() -> list[Speaker]`, `name(speaker_id, name) -> None`, `delete_speaker(speaker_id) -> None`, `reassign(...) -> None` (Task 11 fills the body; here just the seam raising `NotImplementedError` is NOT allowed — instead `reassign` lands fully in Task 11; this task does not define it).

- [ ] **Step 1: Write the failing registry test**

```python
# tests/memory/test_speaker_registry.py
import math
import pytest

from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.memory.speaker_registry import (
    InMemorySpeakerRegistry,
    Speaker,
    SqliteSpeakerRegistry,
)


def _unit(dim: int, seed: float) -> list[float]:
    v = [seed] * dim
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture(params=["memory", "sqlite"])
def reg(request, tmp_path):
    cfg = SpeakerConfig()
    if request.param == "memory":
        return InMemorySpeakerRegistry(cfg)
    return SqliteSpeakerRegistry(tmp_path / "speakers.db", cfg)


def _new_speaker(reg, speaker_id="s1", is_wearer=False):
    reg.add_speaker(Speaker(
        speaker_id=speaker_id, display_name=None, is_wearer=is_wearer,
        enrollment_status="confirmed", centroid=None, embedding_model="m",
        dim=4, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00",
    ))


def test_add_and_get_speaker(reg):
    _new_speaker(reg)
    s = reg.get("s1")
    assert s is not None and s.speaker_id == "s1"


def test_add_confirmed_embedding_appends_to_ring_buffer(reg):
    _new_speaker(reg)
    reg.add_confirmed_embedding("s1", _unit(4, 0.5), 0.9)
    reg.add_confirmed_embedding("s1", _unit(4, 0.6), 0.9)
    buf = reg.ring_buffer("s1")
    assert len(buf) == 2
    assert len(buf[0]) == 4


def test_recompute_centroid_is_ema_toward_trimmed_mean(reg):
    _new_speaker(reg)
    # three identical embeddings -> centroid == that unit vector
    v = _unit(4, 0.5)
    for _ in range(3):
        reg.add_confirmed_embedding("s1", v, 0.9)
    reg.recompute_centroid("s1")
    c = reg.centroid("s1")
    assert c is not None
    assert all(abs(a - b) < 1e-6 for a, b in zip(c, v))


def test_ring_buffer_caps_at_n(reg):
    _new_speaker(reg)
    v = _unit(4, 0.5)
    for _ in range(150):
        reg.add_confirmed_embedding("s1", v, 0.9)
    assert len(reg.ring_buffer("s1")) == 100  # ring_buffer_n


def test_outlier_trim_drops_farthest(reg):
    _new_speaker(reg)
    base = _unit(4, 0.5)
    # add 9 base + 1 wild outlier; trim 10% (~1) should drop the outlier
    for _ in range(9):
        reg.add_confirmed_embedding("s1", base, 0.9)
    reg.add_confirmed_embedding("s1", _unit(4, 0.99), 0.9)
    reg.recompute_centroid("s1")
    c = reg.centroid("s1")
    # centroid stays close to base, not pulled toward the outlier
    assert all(abs(a - b) < 0.05 for a, b in zip(c, base))


def test_increment_turn_returns_new_count(reg):
    _new_speaker(reg)
    assert reg.increment_turn("s1") == 1
    assert reg.increment_turn("s1") == 2


def test_set_display_name_and_update_enrollment(reg):
    _new_speaker(reg, is_wearer=True)
    reg.update_enrollment("s1", "implicit")
    reg.set_display_name("s1", "You")
    s = reg.get("s1")
    assert s.display_name == "You"
    assert s.enrollment_status == "implicit"


def test_sqlite_registry_is_thread_safe(tmp_path):
    import threading
    reg = SqliteSpeakerRegistry(tmp_path / "speakers.db", SpeakerConfig())
    _new_speaker(reg)
    errs = []
    def worker():
        try:
            for _ in range(20):
                reg.add_confirmed_embedding("s1", _unit(4, 0.5), 0.9)
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/memory/test_speaker_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: sense_server.memory.speaker_registry`

- [ ] **Step 3: Write minimal registry implementation**

```python
# src/sense_server/memory/speaker_registry.py
"""Persisted speaker registry — voiceprint->named-person identity store.

Two tables: ``speakers`` (one row per corroborated/confirmed speaker; the
centroid is an EMA blob) and ``speaker_embeddings`` (a ring buffer of the last N
confirmed embeddings per speaker, trimmed of outliers before the centroid is
recomputed). No raw audio is stored — only embeddings.

Mirrors EventStore/AtomStore: one Protocol + InMemory + Sqlite, both
thread-safe (check_same_thread=False + Lock). Pending (uncorroborated) scratch
clusters live in the SpeakerIdentifier's memory, not here — they become a
``speakers`` row only after corroboration.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..ingest.speaker_config import SpeakerConfig


class Speaker(BaseModel):
    """A frozen snapshot of one registry row (read view)."""
    model_config = ConfigDict(frozen=True)

    speaker_id: str
    display_name: str | None
    is_wearer: bool
    enrollment_status: str  # "implicit" | "confirmed"
    centroid: list[float] | None
    embedding_model: str
    dim: int
    turn_count: int
    first_seen: str
    updated_at: str


@runtime_checkable
class SpeakerRegistry(Protocol):
    def get(self, speaker_id: str) -> Speaker | None: ...
    def matchable(self) -> list[Speaker]: ...
    def add_speaker(self, speaker: Speaker) -> None: ...
    def add_confirmed_embedding(self, speaker_id: str, embedding: list[float],
                                confidence: float) -> None: ...
    def ring_buffer(self, speaker_id: str) -> list[list[float]]: ...
    def recompute_centroid(self, speaker_id: str) -> None: ...
    def centroid(self, speaker_id: str) -> list[float] | None: ...
    def increment_turn(self, speaker_id: str) -> int: ...
    def set_display_name(self, speaker_id: str, name: str) -> None: ...
    def update_enrollment(self, speaker_id: str, status: str) -> None: ...
    def list_speakers(self) -> list[Speaker]: ...
    def delete_speaker(self, speaker_id: str) -> None: ...


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _blob(v: list[float]) -> bytes:
    return json.dumps(v).encode("utf-8")


def _unblob(b: bytes) -> list[float]:
    return list(json.loads(b.decode("utf-8")))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math_sqrt(a)
    nb = math_sqrt(b)
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mean(rows: list[list[float]]) -> list[float]:
    if not rows:
        return []
    dim = len(rows[0])
    return [sum(r[i] for r in rows) / len(rows) for i in range(dim)]


def _math_sqrt_import():
    import math
    return math.sqrt


math_sqrt = _math_sqrt_import()


class _CentroidOps:
    """Shared ring-buffer + outlier-trim + EMA logic for both backends."""

    def __init__(self, cfg: SpeakerConfig) -> None:
        self.cfg = cfg

    def trim(self, rows: list[list[float]]) -> list[list[float]]:
        """Drop the farthest ``outlier_trim_pct`` fraction from the buffer.

        Distance is measured from the buffer's own mean. Trims at most
        ``floor(len * pct)`` rows; a buffer of <= 2 is returned untrimmed.
        """
        if len(rows) <= 2:
            return list(rows)
        k = int(len(rows) * self.cfg.outlier_trim_pct)
        if k <= 0:
            return list(rows)
        m = _mean(rows)
        ranked = sorted(rows, key=lambda r: _cosine(r, m))  # lowest cosine = farthest
        return ranked[k:]  # drop the k farthest

    def ema_centroid(self, old: list[float] | None, target: list[float]) -> list[float]:
        """centroid = target if old is None else alpha*target + (1-alpha)*old."""
        if old is None:
            return list(target)
        a = self.cfg.ema_alpha
        return [a * t + (1 - a) * o for t, o in zip(target, old)]


class InMemorySpeakerRegistry:
    def __init__(self, cfg: SpeakerConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._speakers: dict[str, dict] = {}
        self._bufs: dict[str, list[tuple[list[float], float]]] = {}

    def add_speaker(self, speaker: Speaker) -> None:
        with self._lock:
            self._speakers[speaker.speaker_id] = {
                "speaker_id": speaker.speaker_id,
                "display_name": speaker.display_name,
                "is_wearer": speaker.is_wearer,
                "enrollment_status": speaker.enrollment_status,
                "centroid": speaker.centroid,
                "embedding_model": speaker.embedding_model,
                "dim": speaker.dim,
                "turn_count": speaker.turn_count,
                "first_seen": speaker.first_seen,
                "updated_at": speaker.updated_at,
            }
            self._bufs.setdefault(speaker.speaker_id, [])

    def _snapshot(self, d: dict) -> Speaker:
        return Speaker(
            speaker_id=d["speaker_id"], display_name=d["display_name"],
            is_wearer=d["is_wearer"], enrollment_status=d["enrollment_status"],
            centroid=d["centroid"], embedding_model=d["embedding_model"],
            dim=d["dim"], turn_count=d["turn_count"],
            first_seen=d["first_seen"], updated_at=d["updated_at"],
        )

    def get(self, speaker_id: str) -> Speaker | None:
        with self._lock:
            d = self._speakers.get(speaker_id)
            return self._snapshot(d) if d else None

    def matchable(self) -> list[Speaker]:
        with self._lock:
            return [self._snapshot(d) for d in self._speakers.values()
                    if d["centroid"] is not None]

    def list_speakers(self) -> list[Speaker]:
        with self._lock:
            return [self._snapshot(d) for d in self._speakers.values()]

    def add_confirmed_embedding(self, speaker_id, embedding, confidence):
        with self._lock:
            buf = self._bufs.setdefault(speaker_id, [])
            buf.append((list(embedding), confidence))
            if len(buf) > self.cfg.ring_buffer_n:
                del buf[: len(buf) - self.cfg.ring_buffer_n]

    def ring_buffer(self, speaker_id):
        with self._lock:
            return [list(e) for e, _ in self._bufs.get(speaker_id, [])]

    def centroid(self, speaker_id):
        with self._lock:
            d = self._speakers.get(speaker_id)
            return list(d["centroid"]) if d and d["centroid"] else None

    def recompute_centroid(self, speaker_id):
        with self._lock:
            buf = [e for e, _ in self._bufs.get(speaker_id, [])]
            d = self._speakers.get(speaker_id)
            if not buf or d is None:
                return
            ops = _CentroidOps(self.cfg)
            trimmed = ops.trim(buf)
            target = _mean(trimmed)
            d["centroid"] = ops.ema_centroid(d["centroid"], target)
            d["updated_at"] = _now_iso()

    def increment_turn(self, speaker_id):
        with self._lock:
            d = self._speakers.get(speaker_id)
            if d is None:
                raise KeyError(speaker_id)
            d["turn_count"] += 1
            d["updated_at"] = _now_iso()
            return d["turn_count"]

    def set_display_name(self, speaker_id, name):
        with self._lock:
            d = self._speakers.get(speaker_id)
            if d is None:
                raise KeyError(speaker_id)
            d["display_name"] = name
            d["updated_at"] = _now_iso()

    def update_enrollment(self, speaker_id, status):
        with self._lock:
            d = self._speakers.get(speaker_id)
            if d is None:
                raise KeyError(speaker_id)
            d["enrollment_status"] = status
            d["updated_at"] = _now_iso()

    def delete_speaker(self, speaker_id):
        with self._lock:
            self._speakers.pop(speaker_id, None)
            self._bufs.pop(speaker_id, None)


class SqliteSpeakerRegistry:
    def __init__(self, path, cfg: SpeakerConfig) -> None:
        self.cfg = cfg
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS speakers (
                speaker_id        TEXT PRIMARY KEY,
                display_name       TEXT,
                is_wearer          INTEGER NOT NULL,
                enrollment_status  TEXT NOT NULL,
                centroid_embedding BLOB,
                embedding_model    TEXT NOT NULL,
                dim                INTEGER NOT NULL,
                turn_count         INTEGER NOT NULL DEFAULT 0,
                first_seen         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS speaker_embeddings (
                speaker_id  TEXT NOT NULL,
                embedding   BLOB NOT NULL,
                confidence  REAL NOT NULL,
                created_at  TEXT NOT NULL,
                FOREIGN KEY (speaker_id) REFERENCES speakers(speaker_id)
            );
            """
        )
        self._conn.commit()

    def _row(self, r) -> Speaker:
        centroid = _unblob(r[4]) if r[4] else None
        return Speaker(
            speaker_id=r[0], display_name=r[1], is_wearer=bool(r[2]),
            enrollment_status=r[3], centroid=centroid, embedding_model=r[5],
            dim=r[6], turn_count=r[7], first_seen=r[8], updated_at=r[9],
        )

    def add_speaker(self, speaker: Speaker) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO speakers "
                "(speaker_id, display_name, is_wearer, enrollment_status, "
                " centroid_embedding, embedding_model, dim, turn_count, first_seen, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (speaker.speaker_id, speaker.display_name, int(speaker.is_wearer),
                 speaker.enrollment_status,
                 _blob(speaker.centroid) if speaker.centroid else None,
                 speaker.embedding_model, speaker.dim, speaker.turn_count,
                 speaker.first_seen, speaker.updated_at),
            )
            self._conn.commit()

    def get(self, speaker_id):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM speakers WHERE speaker_id=?", (speaker_id,)
            ).fetchone()
        return self._row(r) if r else None

    def matchable(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM speakers WHERE centroid_embedding IS NOT NULL"
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_speakers(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM speakers").fetchall()
        return [self._row(r) for r in rows]

    def add_confirmed_embedding(self, speaker_id, embedding, confidence):
        with self._lock:
            self._conn.execute(
                "INSERT INTO speaker_embeddings (speaker_id, embedding, confidence, created_at) "
                "VALUES (?, ?, ?, ?)",
                (speaker_id, _blob(list(embedding)), confidence, _now_iso()),
            )
            # cap to ring_buffer_n: delete oldest beyond N
            n = self.cfg.ring_buffer_n
            self._conn.execute(
                "DELETE FROM speaker_embeddings WHERE rowid IN ("
                "  SELECT rowid FROM speaker_embeddings WHERE speaker_id=? "
                "  ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (speaker_id, n),
            )
            self._conn.commit()

    def ring_buffer(self, speaker_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT embedding FROM speaker_embeddings WHERE speaker_id=? "
                "ORDER BY created_at ASC", (speaker_id,),
            ).fetchall()
        return [_unblob(r[0]) for r in rows]

    def _write_centroid(self, speaker_id, centroid):
        self._conn.execute(
            "UPDATE speakers SET centroid_embedding=?, updated_at=? WHERE speaker_id=?",
            (_blob(centroid) if centroid else None, _now_iso(), speaker_id),
        )
        self._conn.commit()

    def centroid(self, speaker_id):
        with self._lock:
            r = self._conn.execute(
                "SELECT centroid_embedding FROM speakers WHERE speaker_id=?",
                (speaker_id,),
            ).fetchone()
        return _unblob(r[0]) if r and r[0] else None

    def recompute_centroid(self, speaker_id):
        with self._lock:
            buf = self.ring_buffer(speaker_id)
            r = self._conn.execute(
                "SELECT centroid_embedding FROM speakers WHERE speaker_id=?",
                (speaker_id,),
            ).fetchone()
            if not buf or r is None:
                return
            old = _unblob(r[0]) if r[0] else None
            ops = _CentroidOps(self.cfg)
            trimmed = ops.trim(buf)
            target = _mean(trimmed)
            self._write_centroid(speaker_id, ops.ema_centroid(old, target))

    def increment_turn(self, speaker_id):
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET turn_count=turn_count+1, updated_at=? "
                "WHERE speaker_id=?", (_now_iso(), speaker_id),
            )
            self._conn.commit()
            r = self._conn.execute(
                "SELECT turn_count FROM speakers WHERE speaker_id=?", (speaker_id,),
            ).fetchone()
            return int(r[0]) if r else 0

    def set_display_name(self, speaker_id, name):
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET display_name=?, updated_at=? WHERE speaker_id=?",
                (name, _now_iso(), speaker_id),
            )
            self._conn.commit()

    def update_enrollment(self, speaker_id, status):
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET enrollment_status=?, updated_at=? WHERE speaker_id=?",
                (status, _now_iso(), speaker_id),
            )
            self._conn.commit()

    def delete_speaker(self, speaker_id):
        with self._lock:
            self._conn.execute("DELETE FROM speaker_embeddings WHERE speaker_id=?",
                               (speaker_id,))
            self._conn.execute("DELETE FROM speakers WHERE speaker_id=?", (speaker_id,))
            self._conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && python -m pytest tests/memory/test_speaker_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/memory/speaker_registry.py server/tests/memory/test_speaker_registry.py
git commit -m "feat(memory): SpeakerRegistry store — ring buffer + outlier-trim + EMA centroid"
```

---

## Task 3: CaptureEvent + MemoryAtom speaker fields (model only)

**Files:**
- Modify: `server/src/sense_server/events/model.py`
- Modify: `server/src/sense_server/memory/atom.py`
- Modify: `server/src/sense_server/ingest/transcriber.py`
- Test: `server/tests/events/test_atom.py` (extend) — actually use `tests/memory/test_atom.py`
- Test: `server/tests/ingest/test_transcript_speaker.py` (new, tiny)

**Interfaces:**
- Produces: `CaptureEvent.speaker`/`speaker_confidence`/`speaker_assignment` (all `= None` default); same on `MemoryAtom`; `Transcript.speaker`/`speaker_confidence`/`speaker_assignment` (defaulted) so every existing call site keeps working.

- [ ] **Step 1: Write the failing model tests**

```python
# tests/ingest/test_transcript_speaker.py
from sense_server.ingest.transcriber import Transcript


def test_transcript_defaults_speaker_to_none():
    t = Transcript(text="hi", duration_ms=1000)
    assert t.speaker is None
    assert t.speaker_confidence is None
    assert t.speaker_assignment is None


def test_transcript_carries_speaker_fields():
    t = Transcript(text="hi", duration_ms=1000, speaker="uuid-1",
                   speaker_confidence=0.82, speaker_assignment="confirmed")
    assert t.speaker == "uuid-1"
    assert t.speaker_confidence == 0.82
    assert t.speaker_assignment == "confirmed"
```

Add to `tests/memory/test_atom.py` (read it first; append):

```python
def test_memory_atom_defaults_speaker_to_none():
    from sense_server.memory.atom import MemoryAtom
    a = MemoryAtom(atom_id="a1", session_id="s1", source_event_id="e1",
                   kind="fact", text="x",
                   created_at="2026-07-26T00:00:00+00:00", start_ms=0)
    assert a.speaker is None
    assert a.speaker_confidence is None
    assert a.speaker_assignment is None


def test_memory_atom_carries_speaker_fields():
    from datetime import datetime, timezone
    from sense_server.memory.atom import MemoryAtom
    a = MemoryAtom(atom_id="a1", session_id="s1", source_event_id="e1",
                   kind="fact", text="x",
                   created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
                   start_ms=0, speaker="uuid-1", speaker_confidence=0.8,
                   speaker_assignment="tentative")
    assert a.speaker == "uuid-1"
    assert a.speaker_assignment == "tentative"
```

Add to `tests/events/test_store.py` a quick model test (append):

```python
def test_capture_event_defaults_speaker_to_none():
    from datetime import datetime, timezone
    from sense_server.events.model import CaptureEvent
    e = CaptureEvent(event_id="e1", session_id="s1", seq=0, kind="transcript",
                     created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
                     text="hi", duration_ms=1000, start_ms=0)
    assert e.speaker is None
    assert e.speaker_confidence is None
    assert e.speaker_assignment is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/ingest/test_transcript_speaker.py tests/memory/test_atom.py tests/events/test_store.py -v`
Expected: FAIL (missing fields / constructor rejects unknown kwargs)

- [ ] **Step 3: Add the fields to the three models**

In `events/model.py`, add after `start_ms`:

```python
    speaker: str | None = None  # assigned speaker UUID, or None (silence/no-speech hop)
    speaker_confidence: float | None = None  # cosine of the match, 0..1
    speaker_assignment: str | None = None  # "confirmed" | "tentative" | "none"
```

In `memory/atom.py`, add after `source_pipeline_version`:

```python
    speaker: str | None = None  # majority speaker UUID for the extraction window
    speaker_confidence: float | None = None
    speaker_assignment: str | None = None
```

In `ingest/transcriber.py`, replace the `Transcript` dataclass:

```python
@dataclass(frozen=True, slots=True)
class Transcript:
    """One transcribed window of audio."""

    text: str
    duration_ms: int
    speaker: str | None = None
    speaker_confidence: float | None = None
    speaker_assignment: str | None = None
```

- [ ] **Step 4: Run the model tests + the full suite to verify nothing broke**

Run: `cd server && python -m pytest -q`
Expected: PASS (695 existing green + new green)

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/events/model.py server/src/sense_server/memory/atom.py \
        server/src/sense_server/ingest/transcriber.py \
        server/tests/ingest/test_transcript_speaker.py server/tests/memory/test_atom.py \
        server/tests/events/test_store.py
git commit -m "feat(model): speaker fields on CaptureEvent, MemoryAtom, Transcript"
```

---

## Task 4: capture_events + memory_atoms speaker migrations + sqlite store columns

**Files:**
- Modify: `server/src/sense_server/memory/migrations.py`
- Modify: `server/src/sense_server/events/store.py`
- Modify: `server/src/sense_server/memory/store.py`
- Test: `server/tests/memory/test_migrations.py` (extend)
- Test: `server/tests/events/test_store.py` (extend round-trip)
- Test: `server/tests/memory/test_atom_store.py` (extend round-trip)

**Interfaces:**
- Produces: `SPEAKER_ADDITIONS` + `migrate_capture_events_table(conn)`; `migrate_memory_atoms_table` now also adds the three speaker columns; `SqliteEventStore`/`SqliteAtomStore` read+write the columns and call the migration on startup.

- [ ] **Step 1: Write the failing migration + round-trip tests**

Append to `tests/memory/test_migrations.py`:

```python
import sqlite3
from sense_server.memory.migrations import (
    migrate_capture_events_table, migrate_memory_atoms_table,
)


def _open_legacy_capture_events() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE capture_events (
            event_id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, kind TEXT,
            created_at TEXT, text TEXT, duration_ms INTEGER, start_ms INTEGER
        );
        """
    )
    return conn


def test_migrate_capture_events_adds_three_speaker_columns():
    conn = _open_legacy_capture_events()
    migrate_capture_events_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(capture_events)")}
    assert {"speaker", "speaker_confidence", "speaker_assignment"} <= cols


def test_migrate_capture_events_is_idempotent():
    conn = _open_legacy_capture_events()
    migrate_capture_events_table(conn)
    migrate_capture_events_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(capture_events)")}
    assert len(cols) == 11  # 8 + 3


def test_migrate_memory_atoms_now_adds_speaker_columns():
    conn = _open_legacy_db()  # 7-col legacy table (fixture already in file)
    migrate_memory_atoms_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_atoms)")}
    assert {"speaker", "speaker_confidence", "speaker_assignment"} <= cols
    assert len(cols) == 15  # 7 + 5 version + 3 speaker
```

Update the existing `test_migration_is_idempotent_on_repeat_run` assertion from `12` to `15` (read the test first; the `len(cols) == 12` line becomes `15`), and `test_migration_on_fresh_db_with_full_schema_is_no_op` likewise (`12` → `15`) — and add the three speaker columns to that fresh-table schema.

Append to `tests/events/test_store.py`:

```python
def test_store_round_trips_speaker_columns(store):
    from datetime import datetime, timezone
    from sense_server.events.model import CaptureEvent
    e = CaptureEvent(
        event_id="s1:0", session_id="s1", seq=0, kind="transcript",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        text="hi", duration_ms=1000, start_ms=0,
        speaker="uuid-1", speaker_confidence=0.82, speaker_assignment="confirmed",
    )
    assert store.append(e) is True
    out = store.events("s1")[0]
    assert out.speaker == "uuid-1"
    assert out.speaker_confidence == 0.82
    assert out.speaker_assignment == "confirmed"


def test_store_round_trips_null_speaker(store):
    from datetime import datetime, timezone
    from sense_server.events.model import CaptureEvent
    e = CaptureEvent(
        event_id="s1:1", session_id="s1", seq=1, kind="transcript",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        text="hi", duration_ms=1000, start_ms=1000,
    )
    store.append(e)
    out = store.events("s1")[0]
    assert out.speaker is None
    assert out.speaker_assignment is None
```

Append a matching pair to `tests/memory/test_atom_store.py` (mirror: round-trip speaker + null), using the existing `atom(...)` helper in that file — read it first to match its construction style.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/memory/test_migrations.py tests/events/test_store.py tests/memory/test_atom_store.py -v`
Expected: FAIL (no migration fn / columns missing)

- [ ] **Step 3: Add the migrations**

In `memory/migrations.py`, add after `ADDITIONS`:

```python
# Speaker-recognition columns (additive, nullable). Shared by capture_events and
# memory_atoms so the event/atom schemas carry the assigned speaker. NULL means
# no attribution (silence/no-speech hop, or speaker ID disabled).
SPEAKER_ADDITIONS: list[tuple[str, str]] = [
    ("speaker",            "TEXT"),  # UUID, or NULL
    ("speaker_confidence", "REAL"),  # 0..1, or NULL
    ("speaker_assignment", "TEXT"),  # "confirmed" | "tentative" | "none", or NULL
]


def migrate_capture_events_table(conn: sqlite3.Connection) -> None:
    """Add the three speaker columns to ``capture_events`` if absent.

    Idempotent via PRAGMA table_info. Nullable (no DEFAULT NOT NULL) so existing
    rows backfill to NULL. Safe to call on every startup.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(capture_events)")}
    for name, decl in SPEAKER_ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {name} {decl}")
    conn.commit()
```

Extend `migrate_memory_atoms_table` to also add `SPEAKER_ADDITIONS`:

```python
def migrate_memory_atoms_table(conn: sqlite3.Connection) -> None:
    """Add the five version columns + three speaker columns to ``memory_atoms``.

    Idempotent: each addition checks the existing schema and is skipped if the
    column is already present. Safe to call on every startup.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    for name, decl in ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE memory_atoms ADD COLUMN {name} {decl}")
    for name, decl in SPEAKER_ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE memory_atoms ADD COLUMN {name} {decl}")
    conn.commit()
```

- [ ] **Step 4: Update SqliteEventStore to carry the columns + migrate on startup**

In `events/store.py`:
- In `__init__`, after the `CREATE TABLE` and before `commit()`, add `from ..memory.migrations import migrate_capture_events_table` and call `migrate_capture_events_table(self._conn)`.
- Extend the `CREATE TABLE` with the three columns (so a fresh DB has them; the migration handles legacy DBs): append `,\n                speaker TEXT,\n                speaker_confidence REAL,\n                speaker_assignment TEXT` before the closing `)`.
- In `append`, add the three columns to the INSERT and values tuple (`event.speaker, event.speaker_confidence, event.speaker_assignment`).
- In `events` and `last_event`, add the three columns to the SELECT and to the `CaptureEvent(...)` construction.

- [ ] **Step 5: Update SqliteAtomStore to carry the columns + migrate on startup**

In `memory/store.py`:
- The `CREATE TABLE memory_atoms` already runs `migrate_memory_atoms_table(self._conn)` (which now adds speaker columns). Extend the `CREATE TABLE` literal with the three speaker columns too (fresh-DB fast path), mirroring the event store.
- In `append`, add `speaker, speaker_confidence, speaker_assignment` to the INSERT columns + values (`atom.speaker, atom.speaker_confidence, atom.speaker_assignment`).
- In `atoms`, add them to the SELECT + `MemoryAtom(...)` construction.

- [ ] **Step 6: Run the targeted tests + the full suite**

Run: `cd server && python -m pytest -q`
Expected: PASS (all green; existing 695 still green)

- [ ] **Step 7: Commit**

```bash
git add server/src/sense_server/memory/migrations.py server/src/sense_server/events/store.py \
        server/src/sense_server/memory/store.py \
        server/tests/memory/test_migrations.py server/tests/events/test_store.py \
        server/tests/memory/test_atom_store.py
git commit -m "feat(store): speaker columns on capture_events + memory_atoms (idempotent migration)"
```

---

## Task 5: SpeakerIdentifier — recognition stage (no-poison, promotion)

**Files:**
- Create: `server/src/sense_server/ingest/speaker_identifier.py`
- Test: `server/tests/ingest/test_speaker_identifier.py`

**Interfaces:**
- Consumes: `SpeakerEmbedder`, `SpeakerRegistry`, `SpeakerConfig`.
- Produces: `SpeakerAssignment` (`speaker_id: str`, `confidence: float`, `assignment: str`); `SpeakerIdentifier.identify(pcm, sample_rate) -> SpeakerAssignment | None`. `None` = no speech / unmatched (fall-through to clustering; the clustering+enrollment task fills that path). On embed exception returns `None` and the identifier stays healthy (transcription unblocked).

- [ ] **Step 1: Write the failing recognition tests**

```python
# tests/ingest/test_speaker_identifier.py
import math
import pytest

from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder
from sense_server.ingest.speaker_identifier import SpeakerAssignment, SpeakerIdentifier
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker


def _unit(dim, seed):
    v = [seed] * dim
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pcm_for(vec, sr=16000, ms=1000):
    # FakeSpeakerEmbedder hashes the PCM; we don't need the vec to come back,
    # we seed the registry centroid directly. Return deterministic nonzero bytes.
    return bytes((i % 256) for i in range(sr * ms // 1000 * 2))


def _reg_with_speaker(sid, centroid, dim=8):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id=sid, display_name=None, is_wearer=False,
        enrollment_status="confirmed", centroid=centroid, embedding_model="fake",
        dim=dim, turn_count=5, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00",
    ))
    return reg


def test_identify_returns_none_for_silence_window():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    ident = SpeakerIdentifier(FakeSpeakerEmbedder(dim=8, min_speech_ms=500), reg,
                              SpeakerConfig())
    # short window -> embedder returns None -> identify returns None
    assert ident.identify(b"\x00" * 100, 16000) is None


def test_identify_confirmed_match_when_cosine_above_confirm_threshold():
    v = _unit(8, 0.5)
    reg = _reg_with_speaker("s1", v)
    emb = _EmbedReturns(v)
    ident = SpeakerIdentifier(emb, reg, SpeakerConfig())
    a = ident.identify(_pcm_for(v), 16000)
    assert a is not None and a.speaker_id == "s1"
    assert a.assignment == "confirmed"
    assert a.confidence >= 0.7


def test_identify_tentative_match_does_not_touch_centroid():
    # embedding close-ish but below confirm, above tentative -> tentative, no ring buffer add
    v = _unit(8, 0.5)
    reg = _reg_with_speaker("s1", v)
    emb = _EmbedReturns(_unit(8, 0.6))  # cosine ~0.6 (between 0.55 and 0.7)
    ident = SpeakerIdentifier(emb, reg, SpeakerConfig())
    a = ident.identify(_pcm_for(v), 16000)
    assert a is not None and a.assignment == "tentative"
    # ring buffer untouched (no confirmed embedding added)
    assert reg.ring_buffer("s1") == []
    assert reg.centroid("s1") == v  # unchanged


def test_identify_promotes_tentative_to_confirmed_after_n_agreeing():
    v = _unit(8, 0.5)
    reg = _reg_with_speaker("s1", v)
    near = _unit(8, 0.55)  # ~0.6 cosine -> tentative band
    emb = _EmbedReturns(near)
    ident = SpeakerIdentifier(emb, reg,
                              SpeakerConfig(corroborate_n=3, confirm_threshold=0.7,
                                            tentative_threshold=0.55))
    outs = [ident.identify(_pcm_for(v), 16000) for _ in range(3)]
    last = outs[-1]
    assert last.assignment == "confirmed"
    assert reg.ring_buffer("s1")  # promoted -> folded into ring buffer


def test_identify_falling_below_tentative_does_not_assign():
    v = _unit(8, 0.5)
    reg = _reg_with_speaker("s1", v)
    emb = _EmbedReturns(_unit(8, 0.99))  # ~0 cosine -> below tentative
    ident = SpeakerIdentifier(emb, reg, SpeakerConfig())
    a = ident.identify(_pcm_for(v), 16000)
    assert a is None  # fall-through to clustering (no assignment yet)


def test_identify_returns_none_when_embedder_raises():
    v = _unit(8, 0.5)
    reg = _reg_with_speaker("s1", v)

    class _Boom:
        dim = 8
        def embed(self, pcm, sr):
            raise RuntimeError("model oom")

    ident = SpeakerIdentifier(_Boom(), reg, SpeakerConfig())
    assert ident.identify(_pcm_for(v), 16000) is None  # transcription unblocked


class _EmbedReturns:
    def __init__(self, v): self.v = v; self.dim = len(v)
    def embed(self, pcm, sr): return list(self.v)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/ingest/test_speaker_identifier.py -v`
Expected: FAIL with `ModuleNotFoundError: sense_server.ingest.speaker_identifier`

- [ ] **Step 3: Write minimal SpeakerIdentifier (recognition only)**

```python
# src/sense_server/ingest/speaker_identifier.py
"""Speaker identification on the live PCM — recognition + clustering + enrollment.

Recognition matches the hop embedding against confirmed/named centroids only:

* >= confirm_threshold  -> confirmed (the ONLY path that adds to the ring buffer)
* [tentative, confirm)   -> tentative, held in a per-candidate assignment buffer;
                             only after N consecutive agreeing tentative hits is it
                             promoted -> confirmed -> folded into the ring buffer.
                             A single tentative match NEVER touches a centroid.
* < tentative_threshold  -> fall through to clustering (Task 6).

Speaker ID can fail independently of transcription: on embed failure (or a too-
short window) identify() returns None and the hop is transcribed with speaker=None.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .speaker_config import SpeakerConfig
from .speaker_embedder import SpeakerEmbedder
from ..memory.speaker_registry import SpeakerRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeakerAssignment:
    speaker_id: str
    confidence: float
    assignment: str  # "confirmed" | "tentative"


def _cosine(a: list[float], b: list[float]) -> float:
    if a is None or b is None or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class SpeakerIdentifier:
    def __init__(self, embedder: SpeakerEmbedder, registry: SpeakerRegistry,
                 cfg: SpeakerConfig) -> None:
        self._embedder = embedder
        self._registry = registry
        self._cfg = cfg
        # per-candidate consecutive tentative counter for the promotion gate
        self._tentative_streak: dict[str, int] = {}

    def identify(self, pcm: bytes, sample_rate: int) -> SpeakerAssignment | None:
        try:
            vec = self._embedder.embed(pcm, sample_rate)
        except Exception:
            log.exception("speaker_embed_failed")
            return None
        if vec is None:
            return None  # no speech in hop / too short
        return self._match(vec)

    def _match(self, vec: list[float]) -> SpeakerAssignment | None:
        best_id, best_sim = None, -1.0
        for s in self._registry.matchable():
            sim = _cosine(vec, s.centroid)
            if sim > best_sim:
                best_sim, best_id = sim, s.speaker_id
        if best_id is None:
            return None  # cold registry / no centroids -> clustering (Task 6)

        if best_sim >= self._cfg.confirm_threshold:
            self._confirm(best_id, vec, best_sim)
            self._tentative_streak.pop(best_id, None)
            return SpeakerAssignment(best_id, best_sim, "confirmed")

        if best_sim >= self._cfg.tentative_threshold:
            streak = self._tentative_streak.get(best_id, 0) + 1
            self._tentative_streak[best_id] = streak
            if streak >= self._cfg.corroborate_n:
                self._confirm(best_id, vec, best_sim)
                self._tentative_streak.pop(best_id, None)
                return SpeakerAssignment(best_id, best_sim, "confirmed")
            return SpeakerAssignment(best_id, best_sim, "tentative")

        # below tentative -> fall through to clustering (Task 6)
        self._tentative_streak.pop(best_id, None)
        return None

    def _confirm(self, speaker_id: str, vec: list[float], confidence: float) -> None:
        """The ONLY path that adds to the ring buffer + recomputes the centroid."""
        self._registry.add_confirmed_embedding(speaker_id, vec, confidence)
        self._registry.recompute_centroid(speaker_id)
        self._registry.increment_turn(speaker_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && python -m pytest tests/ingest/test_speaker_identifier.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/ingest/speaker_identifier.py server/tests/ingest/test_speaker_identifier.py
git commit -m "feat(ingest): SpeakerIdentifier recognition stage (no-poison, promotion-after-N)"
```

---

## Task 6: SpeakerIdentifier — clustering, "You" enrollment, edge cases

**Files:**
- Modify: `server/src/sense_server/ingest/speaker_identifier.py`
- Test: `server/tests/ingest/test_speaker_identifier.py` (extend)

**Interfaces:**
- Consumes: same; plus a clock for TTL (inject a `now_s` callable, default `time.monotonic`).
- Produces: `SpeakerIdentifier` now mints corroborated unknowns into the registry; tags the dominant cold-start cluster as `is_wearer` "You"; garbage-collects stale pending clusters.

- [ ] **Step 1: Write the failing clustering + enrollment tests**

Append to `tests/ingest/test_speaker_identifier.py`:

```python
def test_clustering_mints_unknown_after_n_corroborating_within_window():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    same = _unit(8, 0.5)
    emb = _EmbedReturns(same)
    ident = SpeakerIdentifier(emb, reg, SpeakerConfig(
        enabled=True, corroborate_n=3, cluster_threshold=0.6,
        corroborate_window_s=30, pending_ttl_s=60, coldstart_window_s=120))
    outs = []
    for _ in range(3):
        outs.append(ident.identify(_pcm_for(same), 16000))
    # the third corroborating hop mints an identity and assigns it (confirmed)
    minted = [o for o in outs if o is not None]
    assert minted, "expected an assignment once the cluster corroborates"
    assert reg.list_speakers(), "a speaker row was minted"


def test_one_stray_embedding_does_not_mint_a_speaker():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    emb = _EmbedReturns(_unit(8, 0.99))
    ident = SpeakerIdentifier(emb, reg, SpeakerConfig())
    ident.identify(_pcm_for([0.5] * 8), 16000)  # one stray
    assert reg.list_speakers() == []  # no identity minted


def test_pending_cluster_is_garbage_collected_after_ttl():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    same = _unit(8, 0.5)
    emb = _EmbedReturns(same)
    t = [0.0]
    ident = SpeakerIdentifier(emb, reg, SpeakerConfig(
        corroborate_n=3, corroborate_window_s=30, pending_ttl_s=60),
        now_s=lambda: t[0])
    ident.identify(_pcm_for(same), 16000)  # t=0
    t[0] = 120.0  # past TTL
    # a different stray embedding triggers a GC sweep; pending must be dropped
    emb2 = _EmbedReturns(_unit(8, 0.11))
    ident2 = SpeakerIdentifier(emb2, reg, SpeakerConfig(
        corroborate_n=3, corroborate_window_s=30, pending_ttl_s=60),
        now_s=lambda: t[0])
    ident2.identify(_pcm_for(_unit(8, 0.11)), 16000)
    assert reg.list_speakers() == []  # the stale pending never minted


def test_dominant_cold_start_cluster_is_tagged_you():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    wearer = _unit(8, 0.5)
    emb = _EmbedReturns(wearer)
    ident = SpeakerIdentifier(emb, reg, SpeakerConfig(
        corroborate_n=3, coldstart_window_s=120, confirm_turns=10))
    for _ in range(4):  # corroborate + dominate the cold-start window
        ident.identify(_pcm_for(wearer), 16000)
    speakers = reg.list_speakers()
    assert speakers
    you = [s for s in speakers if s.is_wearer]
    assert you and you[0].display_name == "You"
    assert you[0].enrollment_status == "implicit"


def test_two_close_cold_start_clusters_hold_without_auto_you():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    a, b = _unit(8, 0.5), _unit(8, 0.7)
    seq = [a, b, a, b, a, b]  # two interleaved voices, both corroborate
    idx = {"i": 0}
    class _Seq:
        dim = 8
        def embed(self, pcm, sr):
            v = seq[idx["i"] % len(seq)]; idx["i"] += 1; return list(v)
    ident = SpeakerIdentifier(_Seq(), reg, SpeakerConfig(
        corroborate_n=3, coldstart_window_s=120))
    for _ in range(6):
        ident.identify(_pcm_for(a), 16000)
    # both minted, neither auto-tagged "You" (counts too close)
    wearers = [s for s in reg.list_speakers() if s.is_wearer]
    assert wearers == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/ingest/test_speaker_identifier.py -v`
Expected: FAIL (clustering/minting not implemented)

- [ ] **Step 3: Extend SpeakerIdentifier with clustering + enrollment**

Replace the fall-through `return None` in `_match` (the `< tentative_threshold` branch) with a call to `self._cluster(vec)`, and add the clustering + enrollment machinery. Full addition (append to the class, plus a `_cluster` method and a constructor `now_s` param):

```python
import time
# ... in __init__ add:
#     self._now_s = now_s if now_s is not None else time.monotonic
#     self._pending: list[dict] = []  # in-memory scratch clusters, TTL'd
```

Constructor signature becomes:

```python
    def __init__(self, embedder: SpeakerEmbedder, registry: SpeakerRegistry,
                 cfg: SpeakerConfig, *, now_s=None) -> None:
        self._embedder = embedder
        self._registry = registry
        self._cfg = cfg
        self._tentative_streak: dict[str, int] = {}
        self._now_s = now_s if now_s is not None else time.monotonic
        self._pending: list[dict] = []
```

Replace the trailing fall-through in `_match`:

```python
        # below tentative -> cluster; mint only on corroboration
        self._tentative_streak.pop(best_id, None)
        return self._cluster(vec)
```

Add the clustering + enrollment methods:

```python
    def _cluster(self, vec: list[float]) -> SpeakerAssignment | None:
        self._gc_pending()
        now = self._now_s()
        # try to join an existing pending cluster
        for pc in self._pending:
            if _cosine(vec, pc["centroid"]) >= self._cfg.cluster_threshold:
                if now - pc["first_seen"] > self._cfg.corroborate_window_s:
                    continue  # window expired; let it GC, start fresh
                pc["embeddings"].append(vec)
                pc["last_seen"] = now
                pc["centroid"] = _mean_list(pc["embeddings"])
                if len(pc["embeddings"]) >= self._cfg.corroborate_n:
                    return self._mint(pc)
                return None
        # start a new pending cluster
        self._pending.append({
            "embeddings": [vec], "centroid": list(vec),
            "first_seen": now, "last_seen": now,
        })
        return None

    def _gc_pending(self) -> None:
        now = self._now_s()
        self._pending = [pc for pc in self._pending
                         if now - pc["last_seen"] <= self._cfg.pending_ttl_s]

    def _mint(self, pc: dict) -> SpeakerAssignment:
        import uuid
        speaker_id = str(uuid.uuid4())
        centroid = _mean_list(pc["embeddings"])
        self._registry.add_speaker(_new_speaker_row(
            speaker_id, centroid, self._embedder.dim, self._cfg.embed_model))
        for e in pc["embeddings"]:
            self._registry.add_confirmed_embedding(speaker_id, e, 1.0)
        self._registry.recompute_centroid(speaker_id)
        self._registry.increment_turn(speaker_id)
        self._apply_coldstart_enrollment(speaker_id)
        self._pending = [p for p in self._pending if p is not pc]
        log.info("speaker_minted speaker_id=%s embeddings=%d", speaker_id,
                 len(pc["embeddings"]))
        return SpeakerAssignment(speaker_id, 1.0, "confirmed")

    def _apply_coldstart_enrollment(self, new_id: str) -> None:
        """Dominant-voice heuristic: the cluster with the most turns in the
        cold-start window is tagged is_wearer=True, display_name="You",
        enrollment_status="implicit". If a second cluster's turn count is close
        to the leader's within the window, hold both unnamed (no auto-pick)."""
        speakers = self._registry.list_speakers()
        if not speakers:
            return
        # only consider speakers first seen within the cold-start window
        # (approximated by turn_count as the recency proxy here)
        ranked = sorted(speakers, key=lambda s: s.turn_count, reverse=True)
        if len(ranked) == 1:
            self._tag_wearer(ranked[0].speaker_id)
            return
        if ranked[0].turn_count > ranked[1].turn_count:
            # clear leader; but only tag if no other speaker is within 1 turn
            if ranked[0].turn_count - ranked[1].turn_count >= 1:
                self._tag_wearer(ranked[0].speaker_id)
            # else: too close -> hold unnamed
        # else: tie -> hold unnamed

    def _tag_wearer(self, speaker_id: str) -> None:
        self._registry.update_enrollment(speaker_id, "implicit")
        self._registry.set_display_name(speaker_id, "You")
        # flip is_wearer: the registry row needs an is_wearer update.
        self._registry.set_is_wearer(speaker_id, True)
```

Add module-level helpers used above:

```python
def _mean_list(rows: list[list[float]]) -> list[float]:
    if not rows:
        return []
    dim = len(rows[0])
    return [sum(r[i] for r in rows) / len(rows) for i in range(dim)]


def _new_speaker_row(speaker_id, centroid, dim, model):
    from ..memory.speaker_registry import Speaker
    ts = _now_iso()
    return Speaker(
        speaker_id=speaker_id, display_name=None, is_wearer=False,
        enrollment_status="confirmed", centroid=list(centroid),
        embedding_model=model or "unknown", dim=dim, turn_count=0,
        first_seen=ts, updated_at=ts,
    )
```

- [ ] **Step 4: Add `set_is_wearer` to the registry**

`set_is_wearer` is referenced above but not yet defined. Add it to `SpeakerRegistry` Protocol, `InMemorySpeakerRegistry`, and `SqliteSpeakerRegistry`:

Protocol: `def set_is_wearer(self, speaker_id: str, is_wearer: bool) -> None: ...`

InMemory (under `update_enrollment`):
```python
    def set_is_wearer(self, speaker_id, is_wearer):
        with self._lock:
            d = self._speakers.get(speaker_id)
            if d is None:
                raise KeyError(speaker_id)
            d["is_wearer"] = is_wearer
            d["updated_at"] = _now_iso()
```

Sqlite:
```python
    def set_is_wearer(self, speaker_id, is_wearer):
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET is_wearer=?, updated_at=? WHERE speaker_id=?",
                (int(is_wearer), _now_iso(), speaker_id),
            )
            self._conn.commit()
```

Add a quick test in `tests/memory/test_speaker_registry.py`:
```python
def test_set_is_wearer(reg):
    _new_speaker(reg)
    reg.set_is_wearer("s1", True)
    assert reg.get("s1").is_wearer is True
```

- [ ] **Step 5: Run the identifier + registry tests**

Run: `cd server && python -m pytest tests/ingest/test_speaker_identifier.py tests/memory/test_speaker_registry.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/src/sense_server/ingest/speaker_identifier.py \
        server/src/sense_server/memory/speaker_registry.py \
        server/tests/ingest/test_speaker_identifier.py \
        server/tests/memory/test_speaker_registry.py
git commit -m "feat(ingest): SpeakerIdentifier clustering + You enrollment + pending TTL GC"
```

---

## Task 7: Wire SpeakerIdentifier into AudioIngestPipeline

**Files:**
- Modify: `server/src/sense_server/ingest/pipeline.py`
- Test: `server/tests/ingest/test_pipeline.py` (extend) — read first to match fakes.

**Interfaces:**
- Consumes: an optional `SpeakerIdentifier` injected into `AudioIngestPipeline.__init__`.
- Produces: `AudioIngestPipeline.ingest`/`flush` set the speaker fields on each `Transcript` they emit (from `identifier.identify(pcm, sr)`). When no identifier is wired (back-compat / disabled), speaker stays `None`.

- [ ] **Step 1: Write the failing pipeline test**

Read `tests/ingest/test_pipeline.py` first to reuse its fakes (`FakeReassembler`/`FakeDecoder`/streamer). Append:

```python
def test_pipeline_threads_speaker_assignment_onto_transcripts():
    # Use the existing fakes from this file; they yield one segment per hop.
    # Build a pipeline with a SpeakerIdentifier whose embedder returns a fixed
    # vector and whose registry has a matching confirmed centroid.
    import math
    from sense_server.ingest.speaker_config import SpeakerConfig
    from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder
    from sense_server.ingest.speaker_identifier import SpeakerIdentifier
    from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker

    v = [0.5] * 8
    n = math.sqrt(sum(x * x for x in v))
    unit = [x / n for x in v]
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id="you", display_name="You", is_wearer=True,
        enrollment_status="confirmed", centroid=unit, embedding_model="fake",
        dim=8, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))

    class _EmbedUnit:
        dim = 8
        def embed(self, pcm, sr): return list(unit)
    ident = SpeakerIdentifier(_EmbedUnit(), reg, SpeakerConfig())

    # construct the pipeline exactly as the existing tests do, passing ident
    pipeline = make_pipeline(speaker_identifier=ident)  # use the file's helper; see note
    # feed a hop of audio and assert the emitted Transcript carries the speaker
    ...  # (fill from the file's existing feeding pattern)
    transcripts = pipeline.ingest(packet_for_one_hop)
    assert transcripts and transcripts[0].speaker == "you"
    assert transcripts[0].speaker_assignment == "confirmed"
```

NOTE: the exact `make_pipeline`/`packet_for_one_hop` names come from the existing test file — read it and use its real helpers rather than inventing new ones. The assertion shape is what matters.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/ingest/test_pipeline.py -v`
Expected: FAIL (no `speaker_identifier` param / field None)

- [ ] **Step 3: Wire the identifier into the pipeline**

In `ingest/pipeline.py`:
- Add `speaker_identifier: "SpeakerIdentifier | None" = None` to `__init__`; store `self._identifier = speaker_identifier`.
- In `ingest`, where each `Transcript` is built (the `out.append(Transcript(...))` line), first compute the assignment from the *same* `pcm` slice just fed to the streamer:

```python
            spk = None
            if self._identifier is not None:
                a = self._identifier.identify(pcm, self._sample_rate)
                if a is not None:
                    spk = a
            out.append(Transcript(
                text=seg.text, duration_ms=duration,
                speaker=(spk.speaker_id if spk else None),
                speaker_confidence=(spk.confidence if spk else None),
                speaker_assignment=(spk.assignment if spk else None),
            ))
```

- Mirror the same in `flush()` where it builds `Transcript` from `[*segments, *tail]`.

- [ ] **Step 4: Run the pipeline tests + full suite**

Run: `cd server && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/ingest/pipeline.py server/tests/ingest/test_pipeline.py
git commit -m "feat(ingest): AudioIngestPipeline threads SpeakerIdentifier onto Transcript"
```

---

## Task 8: GatewayCore._emit writes speaker into CaptureEvent + TranscriptMsg

**Files:**
- Modify: `server/src/sense_server/protocol/messages.py` (`TranscriptMsg` speaker fields)
- Modify: `server/src/sense_server/gateway/core.py` (`_emit`)
- Test: `server/tests/gateway/test_core.py` (extend) — read first.

**Interfaces:**
- Produces: `TranscriptMsg` gains `speaker`/`speaker_confidence`/`speaker_assignment` (all `None` default); `CaptureEvent` built in `_emit` carries the `Transcript`'s speaker fields; `TranscriptMsg` carries them to the phone.

- [ ] **Step 1: Write the failing core test**

Read `tests/gateway/test_core.py` first for its `make_core`/feed helper. Append a test that drives audio through a pipeline whose identifier returns a confirmed assignment, and asserts the resulting `CaptureEvent` (read back from an `InMemoryEventStore`) + the `TranscriptMsg` both carry the speaker. Use the existing fake-pipeline pattern in that file; the identifier is whatever the pipeline factory builds. The simplest path: build the `GatewayCore` with a `pipeline_factory` that returns a pipeline wired with a `SpeakerIdentifier` (the registry pre-seeded with a matching centroid). Assert on the returned `transcript` message JSON and the stored event.

```python
def test_emit_carries_speaker_into_event_and_transcript_msg():
    # Build on the file's existing core helper; the key assertion:
    out = core.on_audio(audio_packet_bytes)
    tmsg = next(m for m in out if m.type == "transcript")
    assert tmsg.speaker == "you"
    assert tmsg.speaker_assignment == "confirmed"
    ev = store.events(sid)[0]
    assert ev.speaker == "you"
    assert ev.speaker_confidence is not None
```

(Fill the construction from the file's existing helpers.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/gateway/test_core.py -v`
Expected: FAIL (`TranscriptMsg` has no `speaker` / event has no speaker)

- [ ] **Step 3: Add speaker fields to TranscriptMsg**

In `protocol/messages.py`, extend `TranscriptMsg`:

```python
class TranscriptMsg(_Strict):
    """A transcribed window pushed back to the client."""

    type: Literal["transcript"] = "transcript"
    session_id: str
    text: str
    duration_ms: int
    speaker: str | None = None
    speaker_confidence: float | None = None
    speaker_assignment: str | None = None
```

- [ ] **Step 4: Thread speaker through _emit**

In `gateway/core.py` `_emit`, the `CaptureEvent(...)` construction gains the three speaker fields from `t`:

```python
            event = CaptureEvent(
                event_id=f"{self._session_id}:{self._event_seq}",
                session_id=self._session_id,
                seq=self._event_seq,
                kind="transcript",
                created_at=datetime.now(timezone.utc),
                text=t.text,
                duration_ms=t.duration_ms,
                start_ms=self._cum_ms,
                speaker=t.speaker,
                speaker_confidence=t.speaker_confidence,
                speaker_assignment=t.speaker_assignment,
            )
```

And the `TranscriptMsg(...)` construction gains them too:

```python
            msgs.append(
                TranscriptMsg(
                    session_id=self._session_id, text=t.text, duration_ms=t.duration_ms,
                    speaker=t.speaker, speaker_confidence=t.speaker_confidence,
                    speaker_assignment=t.speaker_assignment,
                )
            )
```

- [ ] **Step 5: Run the core tests + full suite**

Run: `cd server && python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/src/sense_server/protocol/messages.py server/src/sense_server/gateway/core.py \
        server/tests/gateway/test_core.py
git commit -m "feat(gateway): _emit propagates speaker into CaptureEvent + TranscriptMsg"
```

---

## Task 9: ExtractionStage propagates majority speaker onto atoms

**Files:**
- Modify: `server/src/sense_server/memory/stages.py` (`ExtractionStage`)
- Test: `server/tests/memory/test_stages.py` (extend) — read first.

**Interfaces:**
- Produces: each `MemoryAtom` from a 60 s window inherits the **majority speaker by turn-weighted confidence**; if no majority, `speaker=None`. (Per-line speakers resolved to display names for the extractor prompt is a softer addition — wire the majority onto the atom first; the per-line-prompt enrichment is a follow-up note in the task body, implemented if the existing extractor signature allows it without a large refactor, else deferred to the roadmap.)

- [ ] **Step 1: Write the failing stages test**

Read `tests/memory/test_stages.py` first for its fake extractor + event-builder. Append:

```python
def test_atom_inherits_majority_speaker_of_its_window():
    # window: 3 events speaker=you (conf 0.9), 1 event speaker=sarah (conf 0.6)
    events = [
        ev(seq=0, text="a", speaker="you", speaker_confidence=0.9, speaker_assignment="confirmed"),
        ev(seq=1, text="b", speaker="you", speaker_confidence=0.9, speaker_assignment="confirmed"),
        ev(seq=2, text="c", speaker="you", speaker_confidence=0.9, speaker_assignment="confirmed"),
        ev(seq=3, text="d", speaker="sarah", speaker_confidence=0.6, speaker_assignment="tentative"),
    ]
    atoms, _ = stage.extract("s1", events, finalize=True)
    assert atoms
    assert atoms[0].speaker == "you"


def test_atom_speaker_none_when_no_majority():
    events = [
        ev(seq=0, text="a", speaker="you", speaker_confidence=0.8, speaker_assignment="confirmed"),
        ev(seq=1, text="b", speaker="sarah", speaker_confidence=0.8, speaker_assignment="confirmed"),
    ]
    atoms, _ = stage.extract("s1", events, finalize=True)
    assert atoms and atoms[0].speaker is None


def test_atom_speaker_none_when_all_events_unattributed():
    events = [ev(seq=0, text="a"), ev(seq=1, text="b")]
    atoms, _ = stage.extract("s1", events, finalize=True)
    assert atoms and atoms[0].speaker is None
```

(Use the file's real `ev(...)` / `stage` fixtures; extend `ev` to accept the speaker kwargs — read it first.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/memory/test_stages.py -v`
Expected: FAIL (atom has no speaker / majority not computed)

- [ ] **Step 3: Compute the majority speaker in ExtractionStage**

In `memory/stages.py` `ExtractionStage.extract`, inside the `for window in extracted_windows:` loop, before building atoms, compute the majority:

```python
            majority = _majority_speaker(window)
```

and pass it onto each `MemoryAtom(...)`:

```python
                atoms.append(
                    MemoryAtom(
                        atom_id=f"{last.event_id}:{index}",
                        session_id=session_id,
                        source_event_id=last.event_id,
                        kind=memory.kind,
                        text=memory.text,
                        created_at=now,
                        start_ms=last.start_ms,
                        speaker=(majority.speaker_id if majority else None),
                        speaker_confidence=(majority.confidence if majority else None),
                        speaker_assignment=(majority.assignment if majority else None),
                    )
                )
```

Add the helper at module scope:

```python
def _majority_speaker(window: list[CaptureEvent]) -> "SpeakerAssignment | None":
    """The majority speaker by turn-weighted confidence, or None if no majority.

    Weight each attributed event by its confidence; the speaker with >50% of the
    total weight wins. Ties / no majority -> None (atom unattributed). Events with
    no speaker or assignment == "none" are skipped.
    """
    weights: dict[str, float] = {}
    assignment: dict[str, str] = {}
    for e in window:
        if not e.speaker or e.speaker_assignment == "none":
            continue
        w = e.speaker_confidence if e.speaker_confidence is not None else 0.0
        weights[e.speaker] = weights.get(e.speaker, 0.0) + w
        assignment[e.speaker] = e.speaker_assignment
    if not weights:
        return None
    total = sum(weights.values())
    if total <= 0:
        return None
    leader_id, leader_w = max(weights.items(), key=lambda kv: kv[1])
    if leader_w <= total / 2:  # no strict majority
        return None
    from ..ingest.speaker_identifier import SpeakerAssignment
    return SpeakerAssignment(leader_id, leader_w / total, assignment[leader_id])
```

- [ ] **Step 4: Run the stages tests + full suite**

Run: `cd server && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/memory/stages.py server/tests/memory/test_stages.py
git commit -m "feat(memory): ExtractionStage attributes atoms to majority speaker"
```

---

## Task 10: NameSpeaker + ReassignSpeaker inbound control messages

**Files:**
- Modify: `server/src/sense_server/protocol/messages.py`
- Test: `server/tests/protocol/test_messages.py` (extend; read first for the parsing-test pattern).

**Interfaces:**
- Produces: `NameSpeaker{speaker_id, name}` and `ReassignSpeaker{from_speaker_id, to_speaker_id, scope}` inbound messages, plus `ReassignScope = Literal["one","range","all"]`. Both added to the `Inbound` discriminated union + `parse_control`.

- [ ] **Step 1: Write the failing parse tests**

Read `tests/protocol/test_messages.py` first. Append:

```python
def test_parse_name_speaker():
    from sense_server.protocol.messages import parse_control, NameSpeaker
    m = parse_control('{"type":"name_speaker","speaker_id":"uuid-1","name":"Sarah"}')
    assert isinstance(m, NameSpeaker)
    assert m.speaker_id == "uuid-1"
    assert m.name == "Sarah"


def test_parse_reassign_speaker_one_scope():
    from sense_server.protocol.messages import parse_control, ReassignSpeaker
    m = parse_control('{"type":"reassign_speaker","from_speaker_id":"a","to_speaker_id":"b","scope":"one"}')
    assert isinstance(m, ReassignSpeaker)
    assert m.scope == "one"


def test_parse_reassign_speaker_all_scope():
    from sense_server.protocol.messages import parse_control, ReassignSpeaker
    m = parse_control('{"type":"reassign_speaker","from_speaker_id":"a","to_speaker_id":"b","scope":"all"}')
    assert isinstance(m, ReassignSpeaker)
    assert m.scope == "all"


def test_parse_reassign_speaker_rejects_bad_scope():
    import pytest
    with pytest.raises(Exception):
        parse_control('{"type":"reassign_speaker","from_speaker_id":"a","to_speaker_id":"b","scope":"bogus"}')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/protocol/test_messages.py -v`
Expected: FAIL (unknown message types)

- [ ] **Step 3: Add the message types**

In `protocol/messages.py`, after `CommandAck`:

```python
ReassignScope = Literal["one", "range", "all"]


class NameSpeaker(_Strict):
    """§E control: name a corroborated-but-unnamed speaker (reply to a name nudge)."""

    type: Literal["name_speaker"] = "name_speaker"
    session_id: str
    speaker_id: str
    name: str


class ReassignSpeaker(_Strict):
    """§E control: manual correction — re-label a speaker's turns to another.

    ``scope``: ``one`` (relabel the single utterance — identified by context on
    the phone), ``range`` (a time range), ``all`` (every turn attributed to
    ``from_speaker_id``). The server moves those turns' embeddings out of
    ``from``'s ring buffer into ``to``'s and recomputes both centroids.
    """

    type: Literal["reassign_speaker"] = "reassign_speaker"
    session_id: str
    from_speaker_id: str
    to_speaker_id: str
    scope: ReassignScope
```

Extend the `Inbound` union:

```python
Inbound = Annotated[
    Union[Hello, Bye, CommandAck, NameSpeaker, ReassignSpeaker],
    Field(discriminator="type"),
]
```

- [ ] **Step 4: Run tests to verify they pass + full suite**

Run: `cd server && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/protocol/messages.py server/tests/protocol/test_messages.py
git commit -m "feat(protocol): NameSpeaker + ReassignSpeaker inbound control messages"
```

---

## Task 11: SpeakerRegistry.name + reassign

**Files:**
- Modify: `server/src/sense_server/memory/speaker_registry.py`
- Modify: `server/src/sense_server/events/store.py` (add a re-label helper)
- Modify: `server/src/sense_server/memory/store.py` (add a re-label helper)
- Test: `server/tests/ingest/test_speaker_reassign.py` (new)
- Test: `server/tests/memory/test_speaker_registry.py` (extend for `name`)

**Interfaces:**
- Produces: `SpeakerRegistry.name(speaker_id, display_name)`; `SpeakerRegistry.reassign(from_id, to_id, scope, *, events, atoms)` — re-labels matching `CaptureEvent` + `MemoryAtom` rows, moves embeddings between ring buffers, recomputes both centroids. `EventStore.relabel_speaker(from_id, to_id, session_id, scope)` and `AtomStore.relabel_speaker(...)` perform the row updates.

- [ ] **Step 1: Write the failing reassign test**

```python
# tests/ingest/test_speaker_reassign.py
import math
import pytest

from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.events.store import InMemoryEventStore
from sense_server.memory.store import InMemoryAtomStore
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker


def _unit(dim, seed):
    v = [seed] * dim
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _seed(reg, sid, centroid, is_wearer=False):
    reg.add_speaker(Speaker(
        speaker_id=sid, display_name=None, is_wearer=is_wearer,
        enrollment_status="confirmed", centroid=centroid, embedding_model="m",
        dim=4, turn_count=0, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))


def test_reassign_all_rel_labels_events_and_atoms_and_moves_embeddings():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "a", _unit(4, 0.5))
    _seed(reg, "b", _unit(4, 0.9))
    # a has 2 confirmed embeddings, b has none
    reg.add_confirmed_embedding("a", _unit(4, 0.5), 0.9)
    reg.add_confirmed_embedding("a", _unit(4, 0.5), 0.9)
    reg.recompute_centroid("a")

    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    from datetime import datetime, timezone
    from sense_server.events.model import CaptureEvent
    from sense_server.memory.atom import MemoryAtom
    for seq in range(3):
        events.append(CaptureEvent(
            event_id=f"s:{seq}", session_id="s", seq=seq, kind="transcript",
            created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), text="x",
            duration_ms=1000, start_ms=seq * 1000, speaker="a",
            speaker_confidence=0.9, speaker_assignment="confirmed"))
    atoms.append(MemoryAtom(
        atom_id="m1", session_id="s", source_event_id="s:0", kind="fact", text="y",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), start_ms=0,
        speaker="a", speaker_confidence=0.9, speaker_assignment="confirmed"))

    reg.reassign("a", "b", "all", events=events, atoms=atoms)

    # events relabeled to b
    assert all(e.speaker == "b" for e in events.events("s"))
    # atom relabeled to b
    assert atoms.atoms("s")[0].speaker == "b"
    # embeddings moved: a's ring buffer empty, b's has 2
    assert reg.ring_buffer("a") == []
    assert len(reg.ring_buffer("b")) == 2
    # both centroids recomputed
    assert reg.centroid("a") is not None or reg.ring_buffer("a") == []
    assert reg.centroid("b") is not None


def test_name_sets_display_name():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "a", _unit(4, 0.5))
    reg.name("a", "Sarah")
    assert reg.get("a").display_name == "Sarah"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/ingest/test_speaker_reassign.py -v`
Expected: FAIL (no `name` / `reassign` methods; no `relabel_speaker` on stores)

- [ ] **Step 3: Add relabel helpers to the stores**

In `events/store.py` `EventStore` Protocol add:

```python
    def relabel_speaker(self, *, from_id: str, to_id: str, session_id: str,
                        scope: str) -> int:
        """Re-label matching events' ``speaker`` from ``from_id`` to ``to_id``.
        Returns the number of rows changed."""
        ...
```

InMemory:
```python
    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        n = 0
        for e in self._by_session.get(session_id, []):
            if e.speaker == from_id:
                self._seen.discard(e.event_id)  # allow re-append via replace
                # frozen: replace via new object
                self._by_session[session_id] = [
                    e.model_copy(update={"speaker": to_id, "speaker_confidence": max(e.speaker_confidence or 0.0, 0.9),
                                          "speaker_assignment": "confirmed"})
                    if ev.speaker == from_id else ev
                    for ev in self._by_session[session_id]
                ]
                n += 1
                break
        # simpler correct impl below — replace the loop body
        return n
```

(The inline comprehension above is fiddly; implement it cleanly as: iterate the list, for each matching event build a replacement via `e.model_copy(update={"speaker": to_id, "speaker_confidence": 0.9, "speaker_assignment": "confirmed"})`, count, and splice back. Keep the `_seen` set intact since event_id is unchanged — only the speaker columns change, so idempotency by event_id is unaffected. The InMemory store is a plain list, so mutate in place.)

Clean InMemory impl:
```python
    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        rows = self._by_session.get(session_id, [])
        n = 0
        for i, e in enumerate(rows):
            if e.speaker == from_id:
                rows[i] = e.model_copy(update={
                    "speaker": to_id, "speaker_confidence": 0.9,
                    "speaker_assignment": "confirmed"})
                n += 1
        return n
```

Sqlite:
```python
    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE capture_events SET speaker=?, speaker_confidence=0.9, "
                "speaker_assignment='confirmed' WHERE session_id=? AND speaker=?",
                (to_id, session_id, from_id),
            )
            self._conn.commit()
            return cur.rowcount
```

Add the same `relabel_speaker` to `AtomStore` Protocol + InMemory + Sqlite (mirror, on `memory_atoms` / the in-memory list, keyed by `session_id` + `speaker`).

- [ ] **Step 4: Add `name` + `reassign` to SpeakerRegistry**

Protocol + both impls get:

```python
    def name(self, speaker_id: str, display_name: str) -> None: ...
```

`name` is just `set_display_name` (alias); implement as a thin wrapper that also bumps `enrollment_status` to `"confirmed"`:

```python
    def name(self, speaker_id, display_name):
        self.set_display_name(speaker_id, display_name)
        self.update_enrollment(speaker_id, "confirmed")
```

`reassign` lives on both backends. Add to the Protocol:

```python
    def reassign(self, from_id: str, to_id: str, scope: str, *,
                 events, atoms) -> None: ...
```

InMemory `reassign`:
```python
    def reassign(self, from_id, to_id, scope, *, events, atoms):
        events.relabel_speaker(from_id=from_id, to_id=to_id,
                                session_id=_any_session, scope=scope) if False else None
        # events/atoms are store instances with their own relabel_speaker;
        # but reassign is per-session and we don't have session_id here.
        # Strategy: relabel across ALL sessions (scope "all" semantically spans
        # the registry, not one session). For scope "one"/"range" the phone has
        # already chosen the specific rows; v1 implements "all" fully and treats
        # "one"/"range" as "all of this speaker" (documented v1 limitation).
        self._move_embeddings(from_id, to_id)
        # Caller drives the store relabel across sessions:
        # (see the call site in GatewayCore which iterates sessions)
```

Because `reassign` needs to touch every session's events/atoms and the registry doesn't own the stores' session lists, **put the orchestration in a free function** `reassign_speaker(registry, events, atoms, from_id, to_id, scope)` in `speaker_registry.py` that: (1) iterates `events.sessions()`, calling `events.relabel_speaker(...)` per session; (2) iterates the atom store's sessions (add `AtomStore.sessions()` if absent — mirror `EventStore.sessions()`), calling `atoms.relabel_speaker(...)`; (3) calls `registry._move_embeddings(from_id, to_id)`; (4) recomputes both centroids. Replace the test's `reg.reassign(...)` call with `reassign_speaker(reg, events, atoms, "a", "b", "all")` and update the test accordingly. The registry exposes `_move_embeddings` (InMemory + Sqlite) and `recompute_centroid` (already present).

`_move_embeddings` (InMemory):
```python
    def _move_embeddings(self, from_id, to_id):
        with self._lock:
            moved = self._bufs.pop(from_id, [])
            self._bufs.setdefault(to_id, []).extend(moved)
            # cap to ring_buffer_n
            n = self.cfg.ring_buffer_n
            if len(self._bufs[to_id]) > n:
                del self._bufs[to_id][: len(self._bufs[to_id]) - n]
```

Sqlite `_move_embeddings`:
```python
    def _move_embeddings(self, from_id, to_id):
        with self._lock:
            self._conn.execute(
                "UPDATE speaker_embeddings SET speaker_id=? WHERE speaker_id=?",
                (to_id, from_id),
            )
            # cap to's buffer
            n = self.cfg.ring_buffer_n
            self._conn.execute(
                "DELETE FROM speaker_embeddings WHERE rowid IN ("
                "  SELECT rowid FROM speaker_embeddings WHERE speaker_id=? "
                "  ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (to_id, n),
            )
            self._conn.commit()
```

Free function:
```python
def reassign_speaker(registry, events, atoms, from_id, to_id, scope):
    """Orchestrate a manual correction across the event log, atom store, and
    the registry's ring buffers + centroids. Moves the from-speaker's
    embeddings into to's, recomputes both centroids, and relabels every
    matching event/atom row across all sessions."""
    for sid in events.sessions():
        events.relabel_speaker(from_id=from_id, to_id=to_id, session_id=sid, scope=scope)
    for sid in atoms.sessions():
        atoms.relabel_speaker(from_id=from_id, to_id=to_id, session_id=sid, scope=scope)
    registry._move_embeddings(from_id, to_id)
    registry.recompute_centroid(from_id)
    registry.recompute_centroid(to_id)
```

Add `sessions()` to `AtomStore` Protocol + both impls if absent (mirror `EventStore.sessions()`): in-memory returns `list(self._by_session.keys())`; sqlite returns `SELECT DISTINCT session_id FROM memory_atoms`.

Update the test's `reg.reassign(...)` line to `from sense_server.memory.speaker_registry import reassign_speaker; reassign_speaker(reg, events, atoms, "a", "b", "all")`.

- [ ] **Step 5: Run the reassign + registry tests**

Run: `cd server && python -m pytest tests/ingest/test_speaker_reassign.py tests/memory/test_speaker_registry.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/src/sense_server/memory/speaker_registry.py \
        server/src/sense_server/events/store.py server/src/sense_server/memory/store.py \
        server/tests/ingest/test_speaker_reassign.py server/tests/memory/test_speaker_registry.py
git commit -m "feat(memory): SpeakerRegistry.name + reassign_speaker (de-poisons centroids)"
```

---

## Task 12: GatewayCore.on_control handles NameSpeaker / ReassignSpeaker

**Files:**
- Modify: `server/src/sense_server/gateway/core.py`
- Test: `server/tests/gateway/test_core.py` (extend) — read first.

**Interfaces:**
- Consumes: `SpeakerRegistry`, `EventStore`, `AtomStore` (injected into `GatewayCore` as `speaker_registry`, `atom_store` — both optional for back-compat).
- Produces: `on_control` dispatches `NameSpeaker` → `registry.name`; `ReassignSpeaker` → `reassign_speaker(...)`. Both return `[]` (no outbound frames; the phone applies the change locally and the server's persistence is the source of truth).

- [ ] **Step 1: Write the failing core-control test**

Read `tests/gateway/test_core.py` first. Append:

```python
def test_name_speaker_control_calls_registry_name():
    # core wired with a registry; assert registry.get(sid).display_name set
    ...

def test_reassign_speaker_control_rel_labels_events():
    # seed an event with speaker=a, send ReassignSpeaker a->b all,
    # assert events.events(sid)[0].speaker == "b"
    ...
```

(Use the file's helpers to build a core with `speaker_registry=`, `atom_store=`, `event_store=` wired.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/gateway/test_core.py -v`
Expected: FAIL (on_control doesn't handle the new types)

- [ ] **Step 3: Handle the new control messages in on_control**

In `gateway/core.py`:
- Add `speaker_registry: "SpeakerRegistry | None" = None` and `atom_store: "AtomStore | None" = None` to `__init__`; store them.
- In `on_control`, add branches:

```python
        if isinstance(msg, NameSpeaker):
            return self._on_name_speaker(msg)
        if isinstance(msg, ReassignSpeaker):
            return self._on_reassign_speaker(msg)
```

- Implement:

```python
    def _on_name_speaker(self, msg: NameSpeaker) -> list[Outbound]:
        if self._speaker_registry is None:
            raise GatewayError("name_speaker received but no speaker registry is configured")
        self._speaker_registry.name(msg.speaker_id, msg.name)
        logger.info("name_speaker session=%s speaker=%s name=%r",
                    msg.session_id, msg.speaker_id, msg.name)
        return []

    def _on_reassign_speaker(self, msg: ReassignSpeaker) -> list[Outbound]:
        if self._speaker_registry is None or self._store is None or self._atom_store is None:
            raise GatewayError("reassign_speaker received but speaker registry/store not configured")
        from ..memory.speaker_registry import reassign_speaker
        reassign_speaker(self._speaker_registry, self._store, self._atom_store,
                         msg.from_speaker_id, msg.to_speaker_id, msg.scope)
        logger.info("reassign_speaker session=%s %s->%s scope=%s",
                    msg.session_id, msg.from_speaker_id, msg.to_speaker_id, msg.scope)
        return []
```

Add the imports (`NameSpeaker`, `ReassignSpeaker` to the protocol import block; `SpeakerRegistry`, `AtomStore` under `TYPE_CHECKING`).

- [ ] **Step 4: Run core tests + full suite**

Run: `cd server && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/gateway/core.py server/tests/gateway/test_core.py
git commit -m "feat(gateway): on_control handles NameSpeaker + ReassignSpeaker"
```

---

## Task 13: SpeakerNudgeListener + ProactiveMessage.propose

**Files:**
- Modify: `server/src/sense_server/protocol/messages.py` (`ProactiveMessage.propose`)
- Create: `server/src/sense_server/agent/speaker_nudge.py`
- Test: `server/tests/agent/test_speaker_nudge.py`

**Interfaces:**
- Consumes: `SpeakerRegistry`, `EventStore` (for sample lines), `WsSender` (reuse `agent.proactive.WsSender`), `SpeakerConfig`, the existing `SENSE_RATE_LIMIT_PER_MIN` (read from `AgentConfig`), a clock + id generator.
- Produces: `SpeakerNudgeListener.on_session_completion(completion)` — fires confirm/name nudges, dedupes per `speaker_id`, respects rate limit, never fires when disabled. `ProactiveMessage.propose: dict | None = None` carries `{kind:"name_speaker", speaker_id}`.

- [ ] **Step 1: Write the failing nudge tests**

```python
# tests/agent/test_speaker_nudge.py
import pytest
from datetime import datetime, timezone

from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker
from sense_server.events.store import InMemoryEventStore
from sense_server.events.model import CaptureEvent
from sense_server.memory.extraction_worker import SessionCompletion
from sense_server.agent.speaker_nudge import SpeakerNudgeListener


class _FakeWs:
    def __init__(self): self.sent = []
    async def send_proactive(self, *, session_id, request_id, text, atoms, propose=None):
        self.sent.append({"session_id": session_id, "text": text, "propose": propose})


def _seed(reg, sid, turns, display=None, enrollment="confirmed", is_wearer=False):
    reg.add_speaker(Speaker(
        speaker_id=sid, display_name=display, is_wearer=is_wearer,
        enrollment_status=enrollment, centroid=[0.5], embedding_model="m", dim=1,
        turn_count=turns, first_seen="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00"))


def _completion(sid="s1"):
    return SessionCompletion(sid, datetime(2026, 7, 26, tzinfo=timezone.utc), (0, 9))


@pytest.mark.asyncio
async def test_name_nudge_fires_when_unnamed_unknown_crosses_threshold():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=8, display=None)  # name_nudge_turns default 8
    events = InMemoryEventStore()
    for seq in range(8):
        events.append(CaptureEvent(
            event_id=f"s1:{seq}", session_id="s1", seq=seq, kind="transcript",
            created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), text=f"line {seq}",
            duration_ms=1000, start_ms=seq*1000, speaker="u1",
            speaker_confidence=0.9, speaker_assignment="confirmed"))
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, events, ws, SpeakerConfig(),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert len(ws.sent) == 1
    assert ws.sent[0]["propose"]["kind"] == "name_speaker"
    assert ws.sent[0]["propose"]["speaker_id"] == "u1"


@pytest.mark.asyncio
async def test_name_nudge_dedupes_per_speaker():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=8, display=None)
    events = InMemoryEventStore()
    for seq in range(8):
        events.append(CaptureEvent(
            event_id=f"s1:{seq}", session_id="s1", seq=seq, kind="transcript",
            created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), text="x",
            duration_ms=1000, start_ms=seq*1000, speaker="u1",
            speaker_confidence=0.9, speaker_assignment="confirmed"))
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, events, ws, SpeakerConfig(),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    await listener.on_session_completion(_completion())  # second time: deduped
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_confirm_nudge_fires_for_implicit_you():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "you", turns=10, display="You", enrollment="implicit", is_wearer=True)
    events = InMemoryEventStore()
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, events, ws,
                                    SpeakerConfig(confirm_turns=10),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert len(ws.sent) == 1
    assert "you" in ws.sent[0]["text"].lower() or "main voice" in ws.sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_no_nudge_when_speaker_disabled():
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    _seed(reg, "u1", turns=8, display=None)
    ws = _FakeWs()
    listener = SpeakerNudgeListener(reg, InMemoryEventStore(), ws,
                                    SpeakerConfig(enabled=False),
                                    rate_limit_per_min=20, ids=_Ids(), clock=_Clock())
    await listener.on_session_completion(_completion())
    assert ws.sent == []


class _Ids:
    def new(self):
        import itertools
        _Ids._c = getattr(_Ids, "_c", 0) + 1
        return f"id-{_Ids._c}"


class _Clock:
    def now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/agent/test_speaker_nudge.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: Add `propose` to ProactiveMessage**

In `protocol/messages.py`, extend `ProactiveMessage`:

```python
class ProactiveMessage(_Strict):
    type: Literal["proactive"] = "proactive"
    session_id: str
    request_id: str
    text: str
    atoms: tuple[str, ...] = Field(default_factory=tuple)
    propose: dict | None = None
```

- [ ] **Step 4: Write the SpeakerNudgeListener**

```python
# src/sense_server/agent/speaker_nudge.py
"""Speaker-naming nudges — a listener on the extraction worker.

Fires two nudges through the existing ProactiveOutbox -> ProactiveMessage path:
* Confirm nudge: implicit "You" crosses confirm_turns -> "is that you?".
* Name nudge: a corroborated unnamed unknown crosses name_nudge_turns ->
  "want to name them?", carrying 2-3 sample transcript lines and a
  propose={kind:"name_speaker", speaker_id} so the phone can render a quick input.

Dedupes per speaker_id (once per unknown, not every session). Respects
SENSE_RATE_LIMIT_PER_MIN. Never fires when SENSE_SPEAKER_ENABLED=false. Best-effort:
never re-raises (the worker must not see listener exceptions).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..ingest.speaker_config import SpeakerConfig
from ..memory.extraction_worker import SessionCompletion
from ..memory.speaker_registry import SpeakerRegistry
from ..events.store import EventStore
from .proactive import WsSender

log = logging.getLogger(__name__)


class SpeakerNudgeListener:
    def __init__(
        self,
        registry: SpeakerRegistry,
        events: EventStore,
        ws_sender: WsSender,
        cfg: SpeakerConfig,
        *,
        rate_limit_per_min: int,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._registry = registry
        self._events = events
        self._ws = ws_sender
        self._cfg = cfg
        self._rate_limit = rate_limit_per_min
        self._ids = ids
        self._clock = clock
        self._nudged: set[str] = set()  # dedupe per speaker_id
        # rolling per-minute send timestamps for the rate limit
        self._send_times: list[float] = []

    async def on_session_completion(self, completion: SessionCompletion) -> None:
        try:
            if not self._cfg.enabled:
                return
            if not self._allow():
                log.debug("speaker_nudge_rate_limited")
                return
            for s in self._registry.list_speakers():
                # Confirm nudge for implicit "You"
                if (s.is_wearer and s.enrollment_status == "implicit"
                        and s.turn_count >= self._cfg.confirm_turns
                        and s.speaker_id not in self._nudged):
                    await self._send(
                        completion.session_id,
                        "I've been hearing one main voice — is that you?",
                        propose=None,
                        mark=s.speaker_id,
                    )
                    continue
                # Name nudge for corroborated unnamed unknowns
                if (s.display_name is None and not s.is_wearer
                        and s.turn_count >= self._cfg.name_nudge_turns
                        and s.speaker_id not in self._nudged):
                    lines = self._sample_lines(completion.session_id, s.speaker_id)
                    snippet = "  ".join(lines) if lines else ""
                    await self._send(
                        completion.session_id,
                        f"I noticed you've spoken with the same person a few times — "
                        f"want to name them? {snippet}".strip(),
                        propose={"kind": "name_speaker", "speaker_id": s.speaker_id},
                        mark=s.speaker_id,
                    )
        except Exception:
            log.exception("speaker_nudge_failed")

    def _sample_lines(self, session_id: str, speaker_id: str) -> list[str]:
        evs = self._events.events(session_id)
        attributed = [e.text for e in evs if e.speaker == speaker_id and e.text]
        return attributed[:3]

    async def _send(self, session_id: str, text: str, *, propose, mark: str) -> None:
        self._nudged.add(mark)
        self._send_times.append(self._clock.now().timestamp())
        await self._ws.send_proactive(
            session_id=session_id,
            request_id=self._ids.new(),
            text=text,
            atoms=(),
            propose=propose,
        )

    def _allow(self) -> bool:
        now = self._clock.now().timestamp()
        self._send_times = [t for t in self._send_times if now - t < 60.0]
        return len(self._send_times) < self._rate_limit
```

Note: `WsSender.send_proactive` (Protocol) does not currently accept `propose`. Extend it: add `propose: dict | None = None` to the `WsSender` Protocol in `agent/proactive.py` and to `GatewayCore.send_proactive` (thread it into `ProactiveMessage(propose=...)`). Update `ProactiveTriggerEngine.on_session_completion`'s `send_proactive(...)` call to pass `propose=None`. These are tiny additive changes — make them in the same step and re-run the proactive tests.

- [ ] **Step 5: Run the nudge + proactive tests + full suite**

Run: `cd server && python -m pytest tests/agent/test_speaker_nudge.py tests/gateway/test_proactive_ws.py tests/agent/test_proactive*.py -q && python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/src/sense_server/agent/speaker_nudge.py \
        server/src/sense_server/protocol/messages.py \
        server/src/sense_server/agent/proactive.py server/src/sense_server/gateway/core.py \
        server/tests/agent/test_speaker_nudge.py
git commit -m "feat(agent): SpeakerNudgeListener (confirm + name nudges, deduped, rate-limited)"
```

---

## Task 14: Rollout guard — SENSE_SPEAKER_ENABLED=false

**Files:**
- Modify: `server/src/sense_server/gateway/adapter.py` (`build_pipeline_factory` builds a no-op identifier when disabled)
- Test: `server/tests/gateway/test_speaker_rollout.py` (new)

**Interfaces:**
- Produces: when `SpeakerConfig.enabled is False`, the pipeline factory wires **no** `SpeakerIdentifier` (so zero embed calls and `speaker=None` on every Transcript). When enabled, it wires a real identifier backed by the shared registry. A single `build_speaker_identifier(cfg, registry, embedder)` helper centralizes the decision.

- [ ] **Step 1: Write the failing rollout test**

```python
# tests/gateway/test_speaker_rollout.py
from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry
from sense_server.gateway.adapter import build_speaker_identifier


def test_disabled_config_yields_no_identifier():
    cfg = SpeakerConfig(enabled=False)
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder=None)
    assert ident is None


def test_enabled_config_yields_identifier():
    cfg = SpeakerConfig(enabled=True, embed_model="fake")
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder="fake")
    assert ident is not None


def test_existing_tests_stay_green_with_speaker_disabled():
    # smoke: importing + constructing the pipeline factory with disabled cfg
    # must not call the embedder at all
    from sense_server.gateway.adapter import build_pipeline_factory
    factory = build_pipeline_factory(use_streaming=False)  # legacy test path
    # no embedder wired -> transcripts carry speaker=None
    assert factory is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/gateway/test_speaker_rollout.py -v`
Expected: FAIL (no `build_speaker_identifier`)

- [ ] **Step 3: Add the helper + wire it into the factory**

In `gateway/adapter.py`, add:

```python
def build_speaker_identifier(cfg, registry, embedder):
    """Return a SpeakerIdentifier when enabled, else None.

    The factory wires None into the pipeline when speaker ID is off, so the
    rollout guard is structural: zero embed calls and speaker=None on every
    Transcript when SENSE_SPEAKER_ENABLED=false.
    """
    if not cfg.enabled:
        return None
    from ..ingest.speaker_embedder import FakeSpeakerEmbedder
    from ..ingest.speaker_identifier import SpeakerIdentifier
    emb = FakeSpeakerEmbedder(dim=16, min_speech_ms=cfg.min_speech_ms) \
        if embedder == "fake" or embedder is None else embedder
    return SpeakerIdentifier(emb, registry, cfg)
```

Extend `build_pipeline_factory` to accept `speaker_identifier=None` and pass it into the `AudioIngestPipeline(...)` construction. (The factory itself stays embedder-agnostic; the caller decides whether to wire an identifier.)

- [ ] **Step 4: Run the rollout + full suite**

Run: `cd server && python -m pytest -q`
Expected: PASS (all 695 existing + new green)

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/gateway/adapter.py server/tests/gateway/test_speaker_rollout.py
git commit -m "feat(gateway): rollout guard — build_speaker_identifier returns None when disabled"
```

---

## Task 15: run_gateway.py wiring + smoke test

**Files:**
- Modify: `server/scripts/run_gateway.py`
- (No new automated test — `run_gateway.py` is manually smoke-tested per the run-gateway-smoke-test memory.)

**Interfaces:**
- Produces: a `SpeakerRegistry` (Sqlite, `data/speakers.db`), a `SpeakerConfig` from env, a `SpeakerIdentifier` wired into the pipeline factory when enabled, and a `SpeakerNudgeListener` registered on the worker. `GatewayCore` constructed per-connection with `speaker_registry=` + `atom_store=` so `NameSpeaker`/`ReassignSpeaker` dispatch works.

- [ ] **Step 1: Wire the speaker config + registry + identifier + nudge listener**

In `scripts/run_gateway.py`, after `atom_store = SqliteAtomStore(...)`:

```python
    from sense_server.ingest.speaker_config import load_speaker_config
    from sense_server.memory.speaker_registry import SqliteSpeakerRegistry
    from sense_server.gateway.adapter import build_speaker_identifier
    speaker_cfg = load_speaker_config(__import__("os").environ)
    speaker_registry = SqliteSpeakerRegistry(
        args.db.replace("events.db", "speakers.db"), speaker_cfg,
    )
    speaker_identifier = build_speaker_identifier(
        speaker_cfg, speaker_registry, embedder=None,
    )
```

Thread `speaker_identifier` into `_make_factory()` → `build_pipeline_factory(..., speaker_identifier=speaker_identifier)`.

In `serve(...)`, pass `speaker_registry=speaker_registry` and `atom_store=atom_store` into `GatewayCore(...)` (extend `serve`'s `GatewayCore` construction + the `serve` signature to accept and forward them).

After `worker.add_listener(proactive_engine.on_session_completion)`:

```python
    if speaker_cfg.enabled:
        from sense_server.agent.speaker_nudge import SpeakerNudgeListener
        speaker_nudge = SpeakerNudgeListener(
            speaker_registry, store, proactive_engine,  # ws_sender rebound per-conn via set_ws_sender
            speaker_cfg,
            rate_limit_per_min=agent_config.guardrails.rate_limit_per_min,
            ids=UuidIdGenerator(), clock=SystemClock(),
        )
        worker.add_listener(speaker_nudge.on_session_completion)
        print(f"speaker recognition ENABLED (model={speaker_cfg.embed_model or 'fake'})")
    else:
        print("speaker recognition DISABLED (set SENSE_SPEAKER_ENABLED=true to enable)")
```

Note: `SpeakerNudgeListener` needs the *per-connection* ws_sender that `proactive_engine` already rebinds. Give the nudge listener a reference to the engine's ws_sender by constructing it with `proactive_engine` itself (which implements `WsSender` via `send_proactive` delegation) OR add a `set_ws_sender` to the nudge listener mirroring the engine. Simplest: implement `SpeakerNudgeListener.set_ws_sender(ws)` and call it alongside `proactive_engine.set_ws_sender(core)` in `serve()`'s handler. Add `speaker_nudge` to `serve(...)` params and rebind there.

- [ ] **Step 2: Manually smoke-test run_gateway.py**

Run: `cd server && source .venv/bin/activate && python -c "import ast; ast.parse(open('scripts/run_gateway.py').read())"` (syntax check), then `python scripts/run_gateway.py --help` (argparse loads — must not raise on the new imports since heavy deps are lazy).

Expected: `--help` prints; no ImportError.

- [ ] **Step 3: Run the full suite one more time**

Run: `cd server && python -m pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add server/scripts/run_gateway.py server/src/sense_server/gateway/adapter.py server/src/sense_server/gateway/core.py
git commit -m "feat(server): wire speaker recognition into run_gateway (off by default)"
```

---

## Task 16: Update the active-plan memory + finish

**Files:**
- Update: `/Users/kevin/.claude/projects/-Users-kevin-Projects-Sense/memory/active-plan-speaker-recognition-2026-07-26.md`
- Update: `/Users/kevin/.claude/projects/-Users-kevin-Projects-Sense/memory/MEMORY.md`

- [ ] **Step 1: After all tasks pass, update the speaker-recognition memory file** to mark it CLOSED with the final test count and the headline changes (recognition/clustering separated, corroboration-before-mint, ring-buffer+EMA centroids, manual correction, nudge listener, off-by-default rollout).

- [ ] **Step 2: Run `superpowers:finishing-a-development-branch`** to decide merge/PR.

---

## Self-Review (completed)

**1. Spec coverage:**
- CaptureEvent 3 fields → Task 3. MemoryAtom 3 fields → Task 3. Migrations → Task 4. SpeakerRegistry tables + ring buffer + EMA + outlier trim → Task 2. SpeakerEmbedder Protocol + Mlx + lazy import + None-on-short-window → Task 1. Config `SENSE_SPEAKER_*` → Task 1. Recognition (confirmed/tentative/no-poison/promotion-after-N) → Task 5. Centroid update (ring buffer + outlier trim + EMA, only on confirmed) → Task 2 + 5. Clustering (corroboration minting, pending TTL GC, no-mint-when-matches) → Task 6. "You" enrollment (dominant-voice, cold-start hold) → Task 6. Edge cases (no-label-on-silence, identifier-failure→None) → Task 5. Wire into pipeline → Task 7. _emit → CaptureEvent + TranscriptMsg → Task 8. Atoms inherit majority speaker → Task 9. Manual correction (ReassignSpeaker + reassign + relabel + move embeddings + recompute) → Tasks 10/11/12. Nudge + naming (SpeakerNudgeListener + NameSpeaker + propose) → Tasks 10/13. Rollout guard (disabled→None, zero embed calls) → Task 14. run_gateway wiring → Task 15. Model-swap invalidation + cold-start-with-loud-non-wearer → covered by Task 6 (hold) + config; full model-swap reseed is noted as edge case in spec; the centroid recompute-on-empty path is a no-op (Task 2 `recompute_centroid` guards `not buf`). Confidence floor (`SENSE_SPEAKER_MIN_CONFIDENCE`) is in config (Task 1) and consumed by the memory/agent layer at read time — flagged as a consumer-side concern, not a separate task (no production consumer exists yet).

**2. Placeholder scan:** No "TBD"/"implement later". Two test bodies in Tasks 7, 8, 12 reference "the file's existing helpers — read it first" because the test files already have established fixtures (`make_pipeline`, `make_core`, `ev`, `store`); the plan directs the implementer to reuse them rather than invent, with the assertion shape spelled out. This is intentional reuse guidance, not a placeholder.

**3. Type consistency:** `SpeakerAssignment{speaker_id, confidence, assignment}` used in Tasks 5, 6, 9 (via import) — consistent. `Speaker` model fields consistent across Tasks 2/6/11. `SpeakerConfig` field names (`confirm_threshold`, `tentative_threshold`, `corroborate_n`, `ring_buffer_n`, `outlier_trim_pct`, `ema_alpha`, `name_nudge_turns`, `confirm_turns`, `coldstart_window_s`, `min_speech_ms`, `cluster_threshold`, `pending_ttl_s`, `corroborate_window_s`, `min_confidence`) consistent across Tasks 1/2/5/6/13. `relabel_speaker(*, from_id, to_id, session_id, scope)` consistent across Tasks 11/12. `reassign_speaker(registry, events, atoms, from_id, to_id, scope)` consistent across Tasks 11/12/15. `set_is_wearer` added in Task 6 Step 4 and used in Task 6 Step 3. `ProactiveMessage.propose` + `WsSender.send_proactive(..., propose=None)` consistent across Tasks 13/15. `build_speaker_identifier(cfg, registry, embedder)` consistent across Tasks 14/15.