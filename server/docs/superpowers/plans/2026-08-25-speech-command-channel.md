# Speech→Command Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the confirmed wearer trigger device commands (photo/video/audio) by speaking, with a two-stage detector (keyword-phrase pre-filter + scoped LLM confirmation) that dispatches through the existing rule-based command path and writes a `command_event` memory on device ack.

**Architecture:** A `CommandDetector` hooks the per-transcript event stream in `GatewayCore._emit` (sibling to the extraction enqueuer). Stage 1 is a free, synchronous phrase match over a rolling recent-text buffer. Stage 2 is a single scoped LLM call (sync `ChatModel.complete` via `asyncio.to_thread`) that returns a structured command or null. Confirmed commands run through the existing `StrictCommandValidator` → `StrictCommandGuardrails` → `CommandDispatcher.issue` chain (no Planner, no retrieval). `CommandMemoryWriter` appends a typed `command_event` `MemoryAtom` (atom_id = command_id) from the existing `_on_command_ack` hook. Off by default; gated on a confirmed wearer.

**Tech Stack:** Python 3, aiohttp gateway, pydantic, OpenAI-compatible ChatModel (httpx), existing command/validation/guardrails stack, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-speech-command-channel-design.md`

## Global Constraints

- **No `Co-Authored-By` trailer** on any commit. AI authorship must not be visible on GitHub.
- **Commit only when the user asks.** FF-merge to local main is authorized; push stays user-gated.
- **Off by default.** `OPENRECALL_COMMAND_DETECTOR_ENABLED=false` by default. No detector wired → behavior identical to today.
- **Confirmed-wearer gate is default-true.** `OPENRECALL_COMMAND_REQUIRE_WEARER=true` by default; voice commands fire only for the confirmed wearer.
- **Do not touch `firmware/opensapien_sensor/`** (stale untracked). Canonical firmware dir is `firmware/openrecall_sensor/`.
- **Subagents only use the default model (glm-5.2)** — omit the `model` parameter on any Agent dispatch.
- Stage 2 must never block the audio path: it is scheduled off-thread via the gateway loop; an in-flight cap + cooldown defend the shared Ollama model.
- No Planner, no retrieval in the command path. Reuse the existing validator/guardrails/dispatcher chain the Planner uses in `_dispatch_command` (`planner.py:308-420`).

## File Structure

- **Create** `src/openrecall_server/agent/command_detector.py` — `CommandDetector` (feed → gate → Stage1 → dedup → schedule Stage2 → dispatch) + pure helpers `match_command_phrase`, `stage2_messages`, `parse_stage2_reply`, `derive_idempotency_key`.
- **Create** `src/openrecall_server/agent/command_memory.py` — `CommandMemoryWriter` (appends `command_event` atom on ack).
- **Modify** `src/openrecall_server/agent/config.py` — `CommandDetectorConfig` model + `DEFAULT_COMMAND_PHRASES` + env constants + parsing in `load_agent_config` + a `command` field on `AgentConfig`.
- **Modify** `src/openrecall_server/gateway/core.py` — constructor params `command_detector` / `command_memory_writer`; `_emit` feeds the detector on a stored event; `_on_command_ack` calls the memory writer.
- **Modify** `scripts/run_gateway.py` — construct + wire the detector and memory writer (gated on `OPENRECALL_COMMAND_DETECTOR_ENABLED`).
- **Create** `tests/agent/test_command_detector.py` — Stage 1 + Stage 2 + feed/gate/dedup/dispatch unit tests.
- **Create** `tests/agent/test_command_memory.py` — `CommandMemoryWriter` unit tests.
- **Modify** `tests/agent/test_config.py` — `CommandDetectorConfig` env parsing tests.
- **Create** `tests/gateway/test_core_command_detector.py` — `GatewayCore` integration (feed + ack-memory wiring).

---

### Task 1: CommandDetectorConfig (env-backed config)

**Files:**
- Modify: `src/openrecall_server/agent/config.py`
- Test: `tests/agent/test_config.py`

**Interfaces:**
- Produces: `CommandDetectorConfig` (pydantic, frozen) with fields `enabled: bool`, `require_wearer: bool`, `confidence_threshold: float`, `cooldown_s: float`, `max_inflight: int`, `llm_model: str | None`, `llm_base_url: str | None`, `llm_api_key: str | None`, `phrases: dict[str, str]` (phrase → command_type). Also `DEFAULT_COMMAND_PHRASES: dict[str, str]` constant. `AgentConfig.command: CommandDetectorConfig`. Parsed by `load_agent_config(env)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/agent/test_config.py`)

```python
from openrecall_server.agent.config import (
    AgentConfig,
    CommandDetectorConfig,
    DEFAULT_COMMAND_PHRASES,
    load_agent_config,
)


def test_command_config_defaults_off_and_wearer_gated():
    cfg = load_agent_config({})
    assert cfg.command.enabled is False
    assert cfg.command.require_wearer is True
    assert cfg.command.confidence_threshold == 0.8
    assert cfg.command.cooldown_s == 3.0
    assert cfg.command.max_inflight == 1
    assert cfg.command.llm_model is None
    # The day-one photo/video/audio phrases ship by default.
    assert DEFAULT_COMMAND_PHRASES["take a photo"] == "capture_photo"
    assert DEFAULT_COMMAND_PHRASES["start a video"] == "start_video"
    assert "stop video" in DEFAULT_COMMAND_PHRASES
    # The default map is loaded when no env override is given.
    assert cfg.command.phrases == DEFAULT_COMMAND_PHRASES


def test_command_config_env_overrides():
    cfg = load_agent_config({
        "OPENRECALL_COMMAND_DETECTOR_ENABLED": "true",
        "OPENRECALL_COMMAND_REQUIRE_WEARER": "false",
        "OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD": "0.66",
        "OPENRECALL_COMMAND_COOLDOWN_S": "1.5",
        "OPENRECALL_COMMAND_MAX_INFLIGHT": "2",
        "OPENRECALL_COMMAND_LLM_MODEL": "qwen2.5:3b",
        "OPENRECALL_COMMAND_LLM_BASE_URL": "http://x:8000/v1",
        "OPENRECALL_COMMAND_LLM_API_KEY": "sk-x",
        "OPENRECALL_COMMAND_PHRASES": "capture_photo:take a photo;start_video:roll video",
    })
    assert cfg.command.enabled is True
    assert cfg.command.require_wearer is False
    assert cfg.command.confidence_threshold == 0.66
    assert cfg.command.cooldown_s == 1.5
    assert cfg.command.max_inflight == 2
    assert cfg.command.llm_model == "qwen2.5:3b"
    assert cfg.command.llm_base_url == "http://x:8000/v1"
    assert cfg.command.llm_api_key == "sk-x"
    # The env var fully replaces the default map (explicit override).
    assert cfg.command.phrases == {"take a photo": "capture_photo", "roll video": "start_video"}


def test_command_config_empty_phrases_is_deliberate_override():
    cfg = load_agent_config({"OPENRECALL_COMMAND_PHRASES": ""})
    assert cfg.command.phrases == {}


def test_command_config_bad_values_raise():
    import pytest
    with pytest.raises(ValueError, match="OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD"):
        load_agent_config({"OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD": "nope"})
    with pytest.raises(ValueError, match="OPENRECALL_COMMAND_MAX_INFLIGHT"):
        load_agent_config({"OPENRECALL_COMMAND_MAX_INFLIGHT": "x"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_config.py -k command -x`
Expected: FAIL with `ImportError` for `CommandDetectorConfig` / `DEFAULT_COMMAND_PHRASES`.

- [ ] **Step 3: Implement**

In `src/openrecall_server/agent/config.py`:

```python
# --- command detector --------------------------------------------------------

ENV_COMMAND_ENABLED = "OPENRECALL_COMMAND_DETECTOR_ENABLED"
ENV_COMMAND_REQUIRE_WEARER = "OPENRECALL_COMMAND_REQUIRE_WEARER"
ENV_COMMAND_CONFIDENCE_THRESHOLD = "OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD"
ENV_COMMAND_COOLDOWN_S = "OPENRECALL_COMMAND_COOLDOWN_S"
ENV_COMMAND_MAX_INFLIGHT = "OPENRECALL_COMMAND_MAX_INFLIGHT"
ENV_COMMAND_LLM_MODEL = "OPENRECALL_COMMAND_LLM_MODEL"
ENV_COMMAND_LLM_BASE_URL = "OPENRECALL_COMMAND_LLM_BASE_URL"
ENV_COMMAND_LLM_API_KEY = "OPENRECALL_COMMAND_LLM_API_KEY"
ENV_COMMAND_PHRASES = "OPENRECALL_COMMAND_PHRASES"

# Day-one voice vocabulary: phrase -> device command type. Stage 1 matches
# these phrases (lowercased substring) against a rolling recent-text buffer.
# Bare common words ("stop", "start") are deliberately NOT keys — they appear
# constantly in speech and would defeat the filter.
DEFAULT_COMMAND_PHRASES: dict[str, str] = {
    "take a photo": "capture_photo",
    "take a picture": "capture_photo",
    "snap a photo": "capture_photo",
    "snap a pic": "capture_photo",
    "capture a photo": "capture_photo",
    "record a video": "record_video",
    "start a video": "start_video",
    "start video": "start_video",
    "stop video": "stop_video",
    "stop the video": "stop_video",
    "start audio": "start_audio",
    "start recording audio": "start_audio",
    "stop audio": "stop_audio",
    "flush snapshots": "flush_snapshots",
}


def _parse_phrase_map(name: str, raw: str) -> dict[str, str]:
    """Semicolon-separated ``type:phrase`` pairs -> phrase -> type dict.
    An empty string is a deliberate "no phrases" override (-> {})."""
    raw = raw.strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"{name}={raw!r}: each entry must be 'type:phrase' (got {pair!r})"
            )
        ctype, phrase = pair.split(":", 1)
        ctype = ctype.strip()
        phrase = phrase.strip()
        if not ctype or not phrase:
            raise ValueError(f"{name}={raw!r}: empty type or phrase in {pair!r}")
        out[phrase.lower()] = ctype
    return out


class CommandDetectorConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    require_wearer: bool = True
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    cooldown_s: float = Field(default=3.0, ge=0.0)
    max_inflight: int = Field(default=1, ge=1)
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    phrases: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_COMMAND_PHRASES))
```

Add `command: CommandDetectorConfig = Field(default_factory=CommandDetectorConfig)` to `AgentConfig`.

In `load_agent_config`, after the `asr_kwargs` block, add a `command_kwargs` block parsing each env var with `_parse_bool` / `_parse_float` / `_parse_int` (raising with the env name on bad values), and `phrases` via `_parse_phrase_map(ENV_COMMAND_PHRASES, env[ENV_COMMAND_PHRASES])` when present. Pass `command=CommandDetectorConfig(**command_kwargs)` to the `AgentConfig(...)` constructor.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/agent/test_config.py -k command -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openrecall_server/agent/config.py tests/agent/test_config.py
git commit -m "feat(agent): CommandDetectorConfig + env-backed command-detector settings"
```

---

### Task 2: Stage 1 phrase matcher (pure)

**Files:**
- Create: `src/openrecall_server/agent/command_detector.py`
- Test: `tests/agent/test_command_detector.py`

**Interfaces:**
- Produces: `match_command_phrase(buffer: str, phrases: dict[str, str]) -> str | None` — lowercased substring match, **longest phrase first** so "take a photo" wins over "take a". Returns the matched command type or `None`.

- [ ] **Step 1: Write the failing tests** (new file `tests/agent/test_command_detector.py`)

```python
from openrecall_server.agent.command_detector import match_command_phrase
from openrecall_server.agent.config import DEFAULT_COMMAND_PHRASES

_P = DEFAULT_COMMAND_PHRASES


def test_match_returns_command_type_on_phrase_present():
    assert match_command_phrase("hey take a photo of mine", _P) == "capture_photo"


def test_match_longest_phrase_wins():
    # "take a photo" must beat "take a" if a shorter prefix were ever a key.
    p = {"take a": "x", "take a photo": "capture_photo"}
    assert match_command_phrase("take a photo", p) == "capture_photo"


def test_match_handles_split_phrase_across_rolling_buffer():
    # Imprecise coalescing can split "take a" ... "photo" across sentences;
    # the rolling buffer joins them, so the phrase still matches.
    assert match_command_phrase("so I said take a and then photo please", _P) is None
    # But when the buffer actually contains the contiguous phrase:
    assert match_command_phrase("earlier talk. take a photo now", _P) == "capture_photo"


def test_match_case_insensitive():
    assert match_command_phrase("TAKE A PHOTO", _P) == "capture_photo"


def test_match_bare_common_word_is_not_a_trigger():
    # "stop" alone is not a key, so normal speech mentioning "stop" misses.
    assert match_command_phrase("she stopped the car abruptly", _P) is None
    assert match_command_phrase("I started to think about it", _P) is None


def test_match_returns_none_when_no_phrase():
    assert match_command_phrase("hello there how are you", _P) is None
    assert match_command_phrase("anything", {}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_command_detector.py::test_match_returns_command_type_on_phrase_present -x`
Expected: FAIL with `ImportError` for `command_detector`.

- [ ] **Step 3: Implement** (new file `src/openrecall_server/agent/command_detector.py`)

```python
"""Speech→command detector: two-stage, confirmed-wearer-gated.

Stage 1 is a free synchronous phrase match over a rolling recent-text buffer
(:func:`match_command_phrase`). Stage 2 is a single scoped LLM call that
returns a structured command or null. Confirmed commands dispatch through
the existing ``StrictCommandValidator`` → ``StrictCommandGuardrails`` →
``CommandDispatcher.issue`` chain — no Planner, no retrieval. A command
memory is written on device ack (see :mod:`command_memory`).
"""
from __future__ import annotations


def match_command_phrase(buffer: str, phrases: dict[str, str]) -> str | None:
    """Return the command type whose phrase appears in ``buffer``, longest
    phrase first, or ``None``. Case-insensitive. Bare common words are not
    keys by design (see ``DEFAULT_COMMAND_PHRASES``)."""
    if not phrases:
        return None
    low = buffer.lower()
    # Longest phrase first so a longer key wins over a shorter prefix.
    for phrase in sorted(phrases, key=len, reverse=True):
        if phrase in low:
            return phrases[phrase]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/agent/test_command_detector.py -x`
Expected: PASS (all Stage 1 tests).

- [ ] **Step 5: Commit**

```bash
git add src/openrecall_server/agent/command_detector.py tests/agent/test_command_detector.py
git commit -m "feat(agent): Stage 1 command-phrase matcher"
```

---

### Task 3: CommandDetector.feed — wearer gate + Stage 1 + dedup/cooldown + in-flight

**Files:**
- Modify: `src/openrecall_server/agent/command_detector.py`
- Test: `tests/agent/test_command_detector.py`

**Interfaces:**
- Consumes: `CommandDetectorConfig`, `SpeakerRegistry.get(speaker_id) -> Speaker | None`, an asyncio loop (for `call_soon_threadsafe`). The `CaptureEvent` shape (`speaker`, `speaker_assignment`, `text`, `event_id`, `start_ms`, `session_id`).
- Produces: `class CommandDetector` with `feed(session_id: str, event: CaptureEvent) -> None`. Schedules `_launch_stage2` via the loop. `_launch_stage2` / `_run_stage2` are stubbed here (real Stage 2 in Task 5).

- [ ] **Step 1: Write the failing tests** (append to `tests/agent/test_command_detector.py`)

```python
import asyncio
from types import SimpleNamespace

from openrecall_server.agent.command_detector import CommandDetector
from openrecall_server.agent.config import CommandDetectorConfig, DEFAULT_COMMAND_PHRASES


class FakeSpeakerRegistry:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, speaker_id):
        return self._m.get(speaker_id)


class FakeLoop:
    """Records call_soon_threadsafe callbacks AND invokes them, so the
    monkey-patched ``_launch_stage2`` lambda actually populates ``_launched``.
    Tests that expect no scheduling assert ``loop.calls == []`` (feed returns
    before ever calling this)."""
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, fn, *args):
        self.calls.append((fn, args))
        fn(*args)


def _event(text, speaker=None, assignment=None, event_id="s:0", start_ms=0, session_id="s"):
    return SimpleNamespace(
        session_id=session_id, event_id=event_id, seq=0, text=text,
        speaker=speaker, speaker_assignment=assignment, start_ms=start_ms,
    )


def _speaker(is_wearer):
    return SimpleNamespace(is_wearer=is_wearer, display_name="You")


def _detector(require_wearer=True, loop=None, phrases=None, stage2=None, cooldown=3.0, max_inflight=1):
    cfg = CommandDetectorConfig(
        enabled=True, require_wearer=require_wearer, cooldown_s=cooldown,
        max_inflight=max_inflight, phrases=phrases or dict(DEFAULT_COMMAND_PHRASES),
    )
    d = CommandDetector(
        model=None, dispatcher=None, command_validator=None, capability_provider=None,
        speaker_registry=None, loop=loop or FakeLoop(), config=cfg, ids=None, clock=None,
    )
    # Stage 2 not implemented yet; record its launches.
    d._launched = []
    d._launch_stage2 = lambda sid, ev, ctype, ctx: d._launched.append((sid, ev, ctype, ctx))
    return d


def test_feed_confirmed_wearer_schedules_stage2():
    loop = FakeLoop()
    reg = FakeSpeakerRegistry({"spk-1": _speaker(is_wearer=True)})
    d = _detector(loop=loop)
    d._speaker_registry = reg
    d.feed("s", _event("take a photo", speaker="spk-1", assignment="confirmed"))
    assert len(loop.calls) == 1
    _sid, _ev, ctype, _ctx = d._launched[0]
    assert ctype == "capture_photo"


def test_feed_non_wearer_does_not_schedule():
    loop = FakeLoop()
    reg = FakeSpeakerRegistry({"spk-2": _speaker(is_wearer=False)})
    d = _detector(loop=loop)
    d._speaker_registry = reg
    d.feed("s", _event("take a photo", speaker="spk-2", assignment="confirmed"))
    assert loop.calls == []
    assert d._launched == []


def test_feed_tentative_or_none_speaker_does_not_schedule():
    loop = FakeLoop()
    d = _detector(loop=loop)
    d._speaker_registry = FakeSpeakerRegistry({"spk-1": _speaker(True)})
    d.feed("s", _event("take a photo", speaker="spk-1", assignment="tentative"))
    assert d._launched == []
    d.feed("s", _event("take a photo", speaker=None, assignment=None))
    assert d._launched == []


def test_feed_require_wearer_false_allows_unknown_speaker():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop)
    d._speaker_registry = None  # not required
    d.feed("s", _event("take a photo", speaker=None, assignment=None))
    assert len(d._launched) == 1


def test_feed_no_phrase_match_does_not_schedule():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop)
    d.feed("s", _event("hello there how are you"))
    assert d._launched == []


def test_feed_dedup_within_cooldown_drops_second():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop, cooldown=100.0)
    d.feed("s", _event("take a photo"))
    d.feed("s", _event("take a photo"))
    assert len(d._launched) == 1  # second dropped by cooldown


def test_feed_split_phrase_matches_across_buffer():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop)
    d.feed("s", _event("so I said take a"))
    d.feed("s", _event("photo please"))
    assert len(d._launched) == 1


def test_feed_inflight_cap_drops_when_full():
    loop = FakeLoop()
    d = _detector(require_wearer=False, loop=loop, max_inflight=1, cooldown=100.0)
    d.feed("s", _event("take a photo", event_id="s:1"))
    # Different command type but in-flight is full -> dropped.
    d.feed("s", _event("start a video", event_id="s:2"))
    assert len(d._launched) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_command_detector.py -k feed -x`
Expected: FAIL (`CommandDetector` not importable / not implemented).

- [ ] **Step 3: Implement** (append to `command_detector.py`)

```python
import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_ROLLING_BUFFER_SEGS = 10


class CommandDetector:
    """Two-stage speech→command detector, confirmed-wearer-gated.

    ``feed`` runs synchronously on the audio path: wearer gate → rolling
    buffer → Stage 1 phrase match → cooldown/in-flight dedup → schedule
    Stage 2 off the audio path via the loop. Stage 2 (LLM + dispatch) is
    :meth:`_run_stage2`, implemented in Task 5.
    """

    def __init__(
        self,
        *,
        model,
        dispatcher,
        command_validator,
        capability_provider,
        speaker_registry,
        loop: asyncio.AbstractEventLoop,
        config: CommandDetectorConfig,
        ids,
        clock,
    ) -> None:
        self._model = model
        self._dispatcher = dispatcher
        self._validator = command_validator
        self._caps = capability_provider
        self._speaker_registry = speaker_registry
        self._loop = loop
        self._cfg = config
        self._ids = ids
        self._clock = clock
        # session_id -> deque of recent segment texts (rolling buffer).
        self._buffers: dict[str, deque[str]] = {}
        # session_id -> {command_type: last_candidate_monotonic}
        self._last_candidate: dict[str, dict[str, float]] = {}
        self._inflight: dict[str, int] = {}
        # command_id -> source_event_id (for the ack-time memory).
        self._provenance: dict[str, str] = {}

    def feed(self, session_id: str, event: Any) -> None:
        if not self._cfg.enabled:
            return
        # --- confirmed-wearer gate ---
        if self._cfg.require_wearer:
            spk = self._speaker_registry.get(event.speaker) if (
                event.speaker and self._speaker_registry is not None) else None
            if not (event.speaker and event.speaker_assignment == "confirmed"
                    and spk is not None and spk.is_wearer):
                return
        # --- rolling recent-text buffer ---
        buf = self._buffers.setdefault(session_id, deque(maxlen=_ROLLING_BUFFER_SEGS))
        buf.append(event.text or "")
        joined = " ".join(buf)
        ctype = match_command_phrase(joined, self._cfg.phrases)
        if ctype is None:
            return
        # --- cooldown dedup (per session, per matched command type) ---
        now = time.monotonic()
        last = self._last_candidate.setdefault(session_id, {})
        if now - last.get(ctype, -self._cfg.cooldown_s) < self._cfg.cooldown_s:
            logger.debug("command_detector dedup session=%s type=%s", session_id, ctype)
            return
        # --- in-flight cap (defends the shared Ollama model) ---
        if self._inflight.get(session_id, 0) >= self._cfg.max_inflight:
            logger.info("command_detector in-flight cap session=%s; dropping", session_id)
            return
        last[ctype] = now
        self._inflight[session_id] = self._inflight.get(session_id, 0) + 1
        context = joined  # Stage 2 sees the rolling buffer as context
        self._loop.call_soon_threadsafe(
            self._launch_stage2, session_id, event, ctype, context)

    def _launch_stage2(self, session_id: str, event: Any, ctype: str, context: str) -> None:
        """Runs on the gateway loop; kicks off the async Stage 2 + dispatch."""
        try:
            asyncio.ensure_future(self._run_stage2(session_id, event, ctype, context))
        except RuntimeError:
            # No running loop in some test contexts; fall back to a task.
            asyncio.create_task(self._run_stage2(session_id, event, ctype, context))

    async def _run_stage2(self, session_id: str, event: Any, ctype: str, context: str) -> None:
        """Task 5 implements the LLM confirmation + dispatch."""
        raise NotImplementedError
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/agent/test_command_detector.py -k feed -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openrecall_server/agent/command_detector.py tests/agent/test_command_detector.py
git commit -m "feat(agent): CommandDetector.feed — wearer gate, Stage 1, dedup, in-flight cap"
```

---

### Task 4: Stage 2 prompt builder + reply parser (pure)

**Files:**
- Modify: `src/openrecall_server/agent/command_detector.py`
- Test: `tests/agent/test_command_detector.py`

**Interfaces:**
- Produces: `stage2_messages(context: str, allowed_types: tuple[str, ...]) -> tuple[str, str]` (system, user) and `parse_stage2_reply(raw: str, allowed_types: tuple[str, ...]) -> Stage2Result` where `Stage2Result` is a small dataclass with `command_type: str | None`, `params: dict`, `confidence: float`.

- [ ] **Step 1: Write the failing tests** (append)

```python
import pytest

from openrecall_server.agent.command_detector import parse_stage2_reply, stage2_messages

_ALLOWED = ("capture_photo", "start_video", "stop_video", "start_audio",
            "stop_audio", "record_video", "flush_snapshots")


def test_stage2_messages_lists_types_and_carries_context():
    system, user = stage2_messages("take a photo of mine", _ALLOWED)
    assert "capture_photo" in system
    assert "narration" in system.lower()
    assert "take a photo of mine" in user


def test_parse_valid_command():
    r = parse_stage2_reply('{"command":{"type":"capture_photo","params":{}},"confidence":0.9}', _ALLOWED)
    assert r.command_type == "capture_photo"
    assert r.params == {}
    assert r.confidence == 0.9


def test_parse_null_command():
    r = parse_stage2_reply('{"command":null,"confidence":0.1}', _ALLOWED)
    assert r.command_type is None
    assert r.confidence == 0.1


def test_parse_rejects_invalid_type():
    r = parse_stage2_reply('{"command":{"type":"delete_everything","params":{}},"confidence":0.99}', _ALLOWED)
    assert r.command_type is None  # not in the allowed vocabulary


def test_parse_rejects_malformed_json():
    r = parse_stage2_reply("not json", _ALLOWED)
    assert r.command_type is None
    assert r.confidence == 0.0


def test_parse_rejects_missing_fields():
    r = parse_stage2_reply('{"command":{"type":"capture_photo"}}', _ALLOWED)
    assert r.command_type is None  # missing confidence -> not a valid command


def test_parse_clamps_confidence():
    r = parse_stage2_reply('{"command":{"type":"capture_photo","params":{}},"confidence":1.5}', _ALLOWED)
    assert r.command_type is None  # confidence out of [0,1] -> reject
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_command_detector.py -k "stage2_messages or parse" -x`
Expected: FAIL (`ImportError` for `parse_stage2_reply` / `stage2_messages`).

- [ ] **Step 3: Implement** (append to `command_detector.py`)

```python
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Stage2Result:
    command_type: str | None
    params: dict
    confidence: float


_SYSTEM_TEMPLATE = (
    "You classify whether a wearer is directly commanding their own wearable "
    "device. Narration, quotes, questions, hypotheticals, and third-person "
    "mentions are NOT commands. If it is a direct command, output JSON "
    '{{"command": {{"type": <one of: {types}>, "params": {{}}}}, '
    '"confidence": <0..1>}}. If not, output '
    '{{"command": null, "confidence": <0..1>}}. params is always {{}} for these '
    "types. Output only the JSON."
)


def stage2_messages(context: str, allowed_types: tuple[str, ...]) -> tuple[str, str]:
    system = _SYSTEM_TEMPLATE.format(types=", ".join(allowed_types))
    user = f"Recent transcript:\n{context}\n\nClassify the last sentence."
    return system, user


def parse_stage2_reply(raw: str, allowed_types: tuple[str, ...]) -> Stage2Result:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return Stage2Result(None, {}, 0.0)
    if not isinstance(obj, dict):
        return Stage2Result(None, {}, 0.0)
    conf = obj.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        return Stage2Result(None, {}, 0.0)
    cmd = obj.get("command")
    if cmd is None:
        return Stage2Result(None, {}, float(conf))
    if not isinstance(cmd, dict):
        return Stage2Result(None, {}, float(conf))
    ctype = cmd.get("type")
    params = cmd.get("params", {})
    if ctype not in allowed_types or not isinstance(params, dict):
        return Stage2Result(None, {}, float(conf))
    return Stage2Result(ctype, params, float(conf))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/agent/test_command_detector.py -k "stage2_messages or parse" -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/openrecall_server/agent/command_detector.py tests/agent/test_command_detector.py
git commit -m "feat(agent): Stage 2 prompt builder + strict reply parser"
```

---

### Task 5: Stage 2 LLM confirmation + rule-based dispatch

**Files:**
- Modify: `src/openrecall_server/agent/command_detector.py`
- Test: `tests/agent/test_command_detector.py`

**Interfaces:**
- Consumes: `ChatModel.complete(system, user) -> str` (sync); `StrictCommandValidator.validate(IssueCommandPayload) -> CommandValidationResult`; `CapabilityProvider.capabilities() / .resources()`; `StrictCommandGuardrails(capabilities=, resources=, confidence_autonomous=).check(ValidatedCommand) -> CommandGuardrailsResult`; `CommandDispatcher.issue(Command) -> SignedCommand`; `UuidIdGenerator.new()`; `Clock.now()`; the command TTL (`planner._default_ttl()`).
- Produces: implemented `_run_stage2`, plus `derive_idempotency_key(session_id, command_type, text)` helper. Records `self._provenance[command_id] = event.event_id`.

- [ ] **Step 1: Write the failing tests** (append)

```python
from openrecall_server.agent.command_detector import CommandDetector, derive_idempotency_key
from openrecall_server.agent.config import CommandDetectorConfig, DEFAULT_COMMAND_PHRASES
from openrecall_server.contracts.clock import FakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.contracts.types import IssueCommandPayload


class FakeChatModel:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self._reply


class FakeValidator:
    def __init__(self, reject=False):
        self._reject = reject

    def validate(self, payload):
        from openrecall_server.agent.validator_command import (
            CommandValidationResult, ValidatedCommand)
        if self._reject:
            return CommandValidationResult(command=None, rejection="unknown", message="no")
        return CommandValidationResult(
            command=ValidatedCommand(
                command_type=payload.command_type, params=payload.params,
                idempotency_key=payload.idempotency_key, confidence=payload.confidence),
            rejection=None, message=None)


class FakeCaps:
    def capabilities(self):
        from openrecall_server.contracts.capability import CapabilitySet
        return CapabilitySet(camera=True, microphone=True, retrospective_buffer=True)

    def resources(self):
        from openrecall_server.commands.resource import DeviceResourceStatus
        return DeviceResourceStatus(battery=0.9, storage_free=1.0, camera_in_use=False)


class FakeDispatcher:
    def __init__(self):
        self.issued = []

    def issue(self, command):
        self.issued.append(command)
        from openrecall_server.commands.signing import SignedCommand
        return SignedCommand(command=command, signature=b"")


def _stage2_detector(reply, reject=False):
    cfg = CommandDetectorConfig(enabled=True, require_wearer=False, confidence_threshold=0.8,
                                phrases=dict(DEFAULT_COMMAND_PHRASES))
    loop = FakeLoop()
    d = CommandDetector(
        model=FakeChatModel(reply), dispatcher=FakeDispatcher(), command_validator=FakeValidator(reject),
        capability_provider=FakeCaps(), speaker_registry=None, loop=loop, config=cfg,
        ids=DeterministicIdGenerator(["cmd-1"]), clock=FakeClock(),
    )
    return d, loop


async def test_stage2_confirmed_command_dispatches():
    d, _ = _stage2_detector('{"command":{"type":"capture_photo","params":{}},"confidence":0.9}')
    ev = _event("take a photo", event_id="s:7")
    await d._run_stage2("s", ev, "capture_photo", "take a photo")
    assert len(d._dispatcher.issued) == 1
    assert d._dispatcher.issued[0].type == "capture_photo"
    assert d._provenance["cmd-1"] == "s:7"
    assert d._inflight.get("s", 0) == 0  # decremented after completion


async def test_stage2_null_command_does_not_dispatch():
    d, _ = _stage2_detector('{"command":null,"confidence":0.1}')
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []


async def test_stage2_low_confidence_does_not_dispatch():
    d, _ = _stage2_detector('{"command":{"type":"capture_photo","params":{}},"confidence":0.5}')
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []


async def test_stage2_validator_rejection_does_not_dispatch():
    d, _ = _stage2_detector('{"command":{"type":"capture_photo","params":{}},"confidence":0.9}', reject=True)
    await d._run_stage2("s", _event("take a photo"), "capture_photo", "take a photo")
    assert d._dispatcher.issued == []


def test_derive_idempotency_key_is_stable_and_typespecific():
    k1 = derive_idempotency_key("s", "capture_photo", "take a photo of mine")
    k2 = derive_idempotency_key("s", "capture_photo", "take a photo of mine")
    k3 = derive_idempotency_key("s", "start_video", "take a photo of mine")
    assert k1 == k2
    assert k1 != k3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_command_detector.py -k "stage2_confirmed or stage2_null or stage2_low or stage2_validator or derive_idempotency" -x`
Expected: FAIL (`_run_stage2` raises `NotImplementedError`; `derive_idempotency_key` missing).

- [ ] **Step 3: Implement** (append to `command_detector.py`)

```python
import hashlib
import re

from .planner import _default_ttl

_ALLOWED_TYPES = (
    "capture_photo", "record_video", "start_audio", "stop_audio",
    "request_buffer", "record_audio", "start_video", "stop_video",
    "flush_snapshots", "sleep", "set_snapshot_interval",
)


def derive_idempotency_key(session_id: str, command_type: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    h = hashlib.sha1(f"{session_id}|{command_type}|{norm}".encode()).hexdigest()[:16]
    return f"voice:{h}"


class CommandDetector:
    # ... (unchanged __init__ / feed / _launch_stage2 from Task 3) ...

    async def _run_stage2(self, session_id, event, ctype, context):
        try:
            system, user = stage2_messages(context, _ALLOWED_TYPES)
            raw = await asyncio.to_thread(self._model.complete, system, user)
            result = parse_stage2_reply(raw, _ALLOWED_TYPES)
            if result.command_type is None:
                return
            if result.confidence < self._cfg.confidence_threshold:
                logger.info("command_detector low confidence %s type=%s",
                            result.confidence, result.command_type)
                return
            self._dispatch(session_id, event, result)
        except Exception:
            logger.exception("command_detector stage2 failed session=%s", session_id)
        finally:
            self._inflight[session_id] = max(0, self._inflight.get(session_id, 0) - 1)

    def _dispatch(self, session_id, event, result):
        from ..contracts.types import IssueCommandPayload
        from .guardrails_command import StrictCommandGuardrails
        from ..commands.model import Command

        idem = derive_idempotency_key(session_id, result.command_type, context_buf(event))
        payload = IssueCommandPayload(
            command_type=result.command_type, params=result.params,
            idempotency_key=idem, confidence=result.confidence,
        )
        v_out = self._validator.validate(payload)
        if v_out.rejection is not None:
            logger.info("command_detector validator rejected: %s", v_out.message)
            return
        guardrails = StrictCommandGuardrails(
            capabilities=self._caps.capabilities(),
            resources=self._caps.resources(),
            confidence_autonomous=self._cfg.confidence_threshold,
        )
        g_out = guardrails.check(v_out.command)
        if not g_out.allowed:
            logger.info("command_detector guardrails refused: %s", g_out.message)
            return
        command = Command(
            command_id=self._ids.new(),
            session_id=session_id,
            type=v_out.command.command_type,
            params=v_out.command.params,
            issued_at=self._clock.now(),
            expires_at=self._clock.now() + _default_ttl(),
            idempotency_key=v_out.command.idempotency_key,
        )
        signed = self._dispatcher.issue(command)
        self._provenance[signed.command.command_id] = event.event_id
        logger.info("command_detector issued command=%s type=%s source=%s",
                    signed.command.command_id, command.type, event.event_id)


def context_buf(event):
    return getattr(event, "text", "") or ""
```

Note: import `_default_ttl` from `.planner` at module top (move it out of the method). If `_default_ttl` is private, the implementer may instead import the TTL constant it wraps — keep behavior identical to the Planner.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/agent/test_command_detector.py -x`
Expected: PASS (all Stage 1 + feed + Stage 2 + dispatch tests).

- [ ] **Step 5: Commit**

```bash
git add src/openrecall_server/agent/command_detector.py tests/agent/test_command_detector.py
git commit -m "feat(agent): Stage 2 LLM confirmation + rule-based command dispatch"
```

---

### Task 6: CommandMemoryWriter (command_event atom on ack)

**Files:**
- Create: `src/openrecall_server/agent/command_memory.py`
- Test: `tests/agent/test_command_memory.py`

**Interfaces:**
- Consumes: `AtomStore.append(MemoryAtom) -> bool` (idempotent by `atom_id`); `Clock.now()`; an `IdGenerator` for `atom_id` (fallback; see below). `CommandType`-to-text mapping for the human record.
- Produces: `class CommandMemoryWriter` with `on_ack(command_id, session_id, command_type, source_event_id, trigger_text) -> None`. Writes a `MemoryAtom` with `atom_id=command_id`, `kind="command_event"`, `source_pipeline_version="command"`.

- [ ] **Step 1: Write the failing tests** (new file `tests/agent/test_command_memory.py`)

```python
from openrecall_server.agent.command_memory import CommandMemoryWriter
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.contracts.clock import FakeClock


def test_on_ack_writes_command_event_atom():
    store = InMemoryAtomStore()
    w = CommandMemoryWriter(store=store, clock=FakeClock(now="2026-08-25T00:00:00+00:00"))
    w.on_ack(command_id="cmd-1", session_id="s", command_type="capture_photo",
             source_event_id="s:7", trigger_text="take a photo of mine")
    atoms = store.list(session_id="s", limit=10)[0]
    assert len(atoms) == 1
    a = atoms[0]
    assert a.atom_id == "cmd-1"  # command_id is the idempotency key
    assert a.kind == "command_event"
    assert a.source_pipeline_version == "command"
    assert a.source_event_id == "s:7"
    assert a.session_id == "s"
    assert "capture_photo" not in a.text  # human text, not the raw type
    assert "photo" in a.text.lower()
    assert "take a photo of mine" in a.text


def test_on_ack_is_idempotent_on_repeat():
    store = InMemoryAtomStore()
    w = CommandMemoryWriter(store=store, clock=FakeClock(now="2026-08-25T00:00:00+00:00"))
    w.on_ack("cmd-1", "s", "capture_photo", "s:7", "take a photo")
    w.on_ack("cmd-1", "s", "capture_photo", "s:7", "take a photo")  # re-ack
    assert len(store.list(session_id="s", limit=10)[0]) == 1


def test_on_ack_skips_when_no_provenance():
    # A /agent-issued command has no transcript provenance -> no command memory.
    store = InMemoryAtomStore()
    w = CommandMemoryWriter(store=store, clock=FakeClock(now="2026-08-25T00:00:00+00:00"))
    w.on_ack("cmd-9", "s", "capture_photo", source_event_id=None, trigger_text=None)
    assert store.list(session_id="s", limit=10)[0] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_command_memory.py -x`
Expected: FAIL (`ImportError` for `CommandMemoryWriter`).

- [ ] **Step 3: Implement** (new file `src/openrecall_server/agent/command_memory.py`)

```python
"""Command-event memory: one ``command_event`` atom per device-acked command.

Written from :meth:`GatewayCore._on_command_ack` (the device-ack hook), not at
dispatch time — issuing ≠ executing. ``atom_id = command_id`` gives natural
idempotency (one memory per command, even under retry/reissue). ``kind`` is
``command_event`` and ``source_pipeline_version`` is ``command`` so these are
distinct from ambient transcript-window memories.
"""
from __future__ import annotations

import logging

from ..memory.atom import MemoryAtom

logger = logging.getLogger(__name__)

_COMMAND_TEXT = {
    "capture_photo": "Took a photo",
    "record_video": "Recorded a video",
    "start_video": "Started a video",
    "stop_video": "Stopped the video",
    "start_audio": "Started audio recording",
    "stop_audio": "Stopped audio recording",
    "flush_snapshots": "Flushed snapshots",
}


class CommandMemoryWriter:
    def __init__(self, *, store, clock, id_generator=None) -> None:
        self._store = store
        self._clock = clock
        self._ids = id_generator

    def on_ack(self, command_id: str, session_id: str, command_type: str,
               source_event_id: str | None, trigger_text: str | None) -> None:
        # Only voice commands carry transcript provenance. A /agent-issued
        # command has no source_event_id -> out of scope for this writer.
        if source_event_id is None:
            return
        label = _COMMAND_TEXT.get(command_type, command_type)
        text = f'{label} (you said: "{trigger_text}")' if trigger_text else label
        atom = MemoryAtom(
            atom_id=command_id,  # idempotency key = command id
            session_id=session_id,
            source_event_id=source_event_id,
            kind="command_event",
            text=text,
            created_at=self._clock.now(),
            start_ms=0,
            source_pipeline_version="command",
        )
        stored = self._store.append(atom)
        if stored:
            logger.info("command_memory wrote command_event command=%s type=%s",
                        command_id, command_type)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/agent/test_command_memory.py -x`
Expected: PASS.

> Retrieval uses `AtomStore.list(*, session_id=, limit=) -> (atoms, anchor)` (verified in `store.py`); `[0]` takes the atoms page. `InMemoryAtomStore.append` dedupes by `atom_id`, so the idempotency test sees one atom.

- [ ] **Step 5: Commit**

```bash
git add src/openrecall_server/agent/command_memory.py tests/agent/test_command_memory.py
git commit -m "feat(agent): CommandMemoryWriter — command_event atom on device ack"
```

---

### Task 7: GatewayCore wiring (feed + ack-memory)

**Files:**
- Modify: `src/openrecall_server/gateway/core.py`
- Test: `tests/gateway/test_core_command_detector.py`

**Interfaces:**
- Consumes: `CommandDetector`, `CommandMemoryWriter` (both None-defaulted). The existing `_emit` stored-event branch and `_on_command_ack` hook.
- Produces: `GatewayCore(command_detector=..., command_memory_writer=...)`. `_emit` calls `command_detector.feed(session_id, event)` after the enqueuer block. `_on_command_ack` resolves the command's `source_event_id` from the detector's provenance and the command type, then calls `command_memory_writer.on_ack(...)`.

- [ ] **Step 1: Write the failing tests** (new file `tests/gateway/test_core_command_detector.py`)

```python
import struct
from datetime import datetime, timedelta, timezone

from openrecall_server.commands.dispatcher import CommandDispatcher
from openrecall_server.commands.model import Command
from openrecall_server.commands.signing import CommandSigner
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.gateway.core import GatewayCore
from openrecall_server.ingest.audio_packet import PacketType, VadState
from openrecall_server.ingest.pipeline import AudioIngestPipeline
from openrecall_server.ingest.reassembler import SessionReassembler
from openrecall_server.protocol.messages import Ack, CommandAck, Hello, TranscriptMsg

NOW = datetime.now(timezone.utc)


class FakeDecoder:
    def decode(self, frame: bytes) -> bytes:
        return b"\x00" * 640


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls += 1
        return f"seg{self.calls}"


def factory(start_seq: int) -> AudioIngestPipeline:
    return AudioIngestPipeline(
        reassembler=SessionReassembler(start_seq=start_seq),
        decoder=FakeDecoder(),
        transcriber=FakeTranscriber(),
        hop_ms=20,
        window_ms=100,
        sample_rate=16000,
    )


def audio_bytes(chunk_seq: int, n_frames: int, vad: int = VadState.SPEECH) -> bytes:
    frames = [bytes([chunk_seq & 0xFF])] * n_frames
    header = struct.pack(
        "<BIIBBB",
        (1 << 4) | PacketType.MEMORY_CHUNK,
        chunk_seq,
        chunk_seq * 20,
        vad,
        len(frames),
        0,
    )
    return header + b"".join(struct.pack("<B", len(f)) + f for f in frames)


class SpyDetector:
    """Records feed() calls; carries a _provenance map like the real detector."""
    def __init__(self):
        self.fed = []
        self._provenance = {}

    def feed(self, session_id, event):
        self.fed.append((session_id, event.event_id, event.text))


class SpyMemoryWriter:
    def __init__(self):
        self.acks = []

    def on_ack(self, command_id, session_id, command_type, source_event_id, trigger_text):
        self.acks.append((command_id, session_id, command_type, source_event_id))


def a_command(command_id: str, session_id: str = "s1") -> Command:
    return Command(
        command_id=command_id, session_id=session_id, type="capture_photo",
        params={}, issued_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )


def test_emit_feeds_detector_on_each_stored_event():
    detector = SpyDetector()
    core = GatewayCore(
        pipeline_factory=factory,
        event_store=InMemoryEventStore(),
        command_detector=detector,
    )
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=5))  # 5 hops -> 5 transcript events

    assert len(detector.fed) == 5
    assert detector.fed[0] == ("s1", "s1:0", "seg1")  # session_id, event_id, text


def test_command_ack_writes_memory_for_provenanced_command():
    detector = SpyDetector()
    memory = SpyMemoryWriter()
    dispatcher = CommandDispatcher(CommandSigner.generate())
    core = GatewayCore(
        pipeline_factory=factory, dispatcher=dispatcher,
        command_detector=detector, command_memory_writer=memory,
    )
    core.on_control(Hello(session_id="s1", start_seq=0))
    dispatcher.issue(a_command("c1"))
    # Simulate the detector having recorded that c1 came from transcript event s1:3.
    detector._provenance["c1"] = "s1:3"

    core.on_control(CommandAck(session_id="s1", command_id="c1"))

    assert memory.acks == [("c1", "s1", "capture_photo", "s1:3")]


def test_command_ack_without_provenance_does_not_write_memory():
    detector = SpyDetector()
    memory = SpyMemoryWriter()
    dispatcher = CommandDispatcher(CommandSigner.generate())
    core = GatewayCore(
        pipeline_factory=factory, dispatcher=dispatcher,
        command_detector=detector, command_memory_writer=memory,
    )
    core.on_control(Hello(session_id="s1", start_seq=0))
    dispatcher.issue(a_command("c2"))
    # No provenance entry for c2 (e.g. an HTTP /agent-issued command) -> no memory.
    core.on_control(CommandAck(session_id="s1", command_id="c2"))
    assert memory.acks == []


def test_emit_with_no_detector_is_a_noop_regression_guard():
    core = GatewayCore(pipeline_factory=factory, event_store=InMemoryEventStore())
    core.on_control(Hello(session_id="s1", start_seq=0))
    out = core.on_audio(audio_bytes(0, n_frames=5))  # must not raise
    # Transcripts still emitted exactly as before the wiring (5 + ack).
    assert len([m for m in out if isinstance(m, TranscriptMsg)]) == 5
    assert [m for m in out if isinstance(m, Ack)] == [Ack(session_id="s1", next_seq=1)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/gateway/test_core_command_detector.py -x`
Expected: FAIL (`GatewayCore` does not accept `command_detector` / `command_memory_writer`).

- [ ] **Step 3: Implement**

In `src/openrecall_server/gateway/core.py`:
- Add constructor params `command_detector=None`, `command_memory_writer=None` and `self._command_detector = command_detector`, `self._command_memory_writer = command_memory_writer`.
- In `_emit`, after the existing enqueuer block (`if stored and self._enqueuer is not None ...`), add:

```python
            if stored and self._command_detector is not None and self._session_id is not None:
                self._command_detector.feed(self._session_id, event)
```

- In `_on_command_ack` (`core.py:408`), reuse the `acked_type` the method ALREADY captures at line 414 (BEFORE `self._dispatcher.ack(...)`). Do NOT re-call `self._command_type(msg.command_id)` after the ack — `ack()` moves the command out of `pending()`, so `_command_type` would return `None` and the memory writer would never fire. After the existing reconciler block, add:

```python
        # --- command memory on ack (speech→command channel) ---
        if self._command_memory_writer is not None and acked_type is not None:
            source_event_id = None
            if self._command_detector is not None:
                source_event_id = self._command_detector._provenance.get(msg.command_id)
            if source_event_id is not None:
                self._command_memory_writer.on_ack(
                    command_id=msg.command_id, session_id=self._session_id or "",
                    command_type=acked_type, source_event_id=source_event_id,
                    trigger_text=None,
                )
```

> `acked_type` is the `str | None` already captured at `core.py:414` (`acked_type = self._command_type(msg.command_id)`) before the ack. `self._session_id` is the bound session. `trigger_text=None` is fine — `CommandMemoryWriter.on_ack` handles `None` (writes the bare label). Voice-command provenance (`source_event_id`) comes from `command_detector._provenance`; HTTP `/agent`-issued commands have no provenance and are skipped (`source_event_id is None`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/gateway/test_core_command_detector.py -x`
Expected: PASS. Also run the full gateway suite as a regression guard:

Run: `python -m pytest tests/gateway/ -x`
Expected: PASS (no regressions from the new optional wiring).

- [ ] **Step 5: Commit**

```bash
git add src/openrecall_server/gateway/core.py tests/gateway/test_core_command_detector.py
git commit -m "feat(gateway): wire CommandDetector + CommandMemoryWriter into _emit and _on_command_ack"
```

---

### Task 8: run_gateway + adapter wiring + manual smoke

**Files:**
- Modify: `scripts/run_gateway.py`
- Modify: `src/openrecall_server/gateway/adapter.py`

**Interfaces:**
- Consumes: `agent_config.command` (`CommandDetectorConfig`), the shared `dispatcher`, `StrictCommandValidator`, `capability_provider`, `speaker_registry`, `atom_store`, the gateway event loop, `UuidIdGenerator`, `SystemClock`, and `llm_chat` (`OpenAICompatibleChatModel`). Task 7 already added `command_detector=None` / `command_memory_writer=None` params to `GatewayCore.__init__`.

> **Per project memory** `run-gateway-smoke-test`: pytest does not import `scripts/run_gateway.py`; this task is wiring + a **manual smoke check**, not a unit test.

> **Loop binding (load-bearing):** `run_gateway.py`'s `main()` is a *sync* function; the event loop is created later by `asyncio.run(main_loop())` (the `await serve(...)` call lives inside the nested `async def main_loop()`). The command detector stores the loop and uses `loop.call_soon_threadsafe(...)` from the audio thread — so it MUST bind the loop `main_loop` actually runs on. Constructing it in sync `main()` with `asyncio.get_event_loop()` would bind a *different* (never-run) loop and Stage 2 would never fire. Therefore the detector is constructed **inside `main_loop()`** with `asyncio.get_running_loop()`, just before `await serve(...)`.

- [ ] **Step 1a: Thread the two kwargs through `serve()` in `adapter.py`**

In `src/openrecall_server/gateway/adapter.py`, add two params to `serve()` (after `settings: "SettingsStore | None" = None,`):

```python
    command_detector=None,
    command_memory_writer=None,
```

and forward them to the per-connection `GatewayCore(...)` construction (the `core = GatewayCore(...)` block inside `handler`), adding after `settings=settings,`:

```python
            command_detector=command_detector,
            command_memory_writer=command_memory_writer,
```

`None` defaults keep every existing caller and test working unchanged.

- [ ] **Step 1b: Construct + wire in `run_gateway.py`**

Inside `async def main_loop()` (NOT sync `main()`), just before the `await serve(...)` call, add a construction block gated on `agent_config.command.enabled`. All referenced names (`agent_config`, `dispatcher`, `llm_chat`, `capability_provider`, `speaker_registry`, `atom_store`, `SystemClock`) are in scope as closures over `main()`'s locals; verify each exists before use. `asyncio` is already imported at module top.

```python
        # Speech→command channel (off by default). Constructed inside main_loop
        # so asyncio.get_running_loop() binds the loop this server runs on —
        # the detector's call_soon_threadsafe must target THIS loop or Stage 2
        # never fires.
        command_detector = None
        command_memory_writer = None
        if agent_config.command.enabled:
            from openrecall_server.agent.command_detector import CommandDetector
            from openrecall_server.agent.command_memory import CommandMemoryWriter
            from openrecall_server.agent.validator_command import StrictCommandValidator
            from openrecall_server.contracts.id_generator import UuidIdGenerator

            # Stage 2 LLM: dedicated model if OPENRECALL_COMMAND_LLM_MODEL is set,
            # else reuse the extractor's shared chat model (llm_chat).
            if agent_config.command.llm_model:
                from openrecall_server.memory.llm import OpenAICompatibleChatModel
                command_llm = OpenAICompatibleChatModel(
                    base_url=agent_config.command.llm_base_url or llm_chat.base_url,
                    model=agent_config.command.llm_model,
                    api_key=agent_config.command.llm_api_key,
                )
            else:
                command_llm = llm_chat

            command_detector = CommandDetector(
                model=command_llm,
                dispatcher=dispatcher,
                command_validator=StrictCommandValidator(),
                capability_provider=capability_provider,  # the same one the Planner uses
                speaker_registry=speaker_registry,
                loop=asyncio.get_running_loop(),
                config=agent_config.command,
                ids=UuidIdGenerator(),
                clock=SystemClock(),
            )
            command_memory_writer = CommandMemoryWriter(
                store=atom_store, clock=SystemClock(),
            )
            logger.info("command detector enabled (require_wearer=%s)",
                        agent_config.command.require_wearer)
        else:
            logger.info("command detector disabled "
                        "(set OPENRECALL_COMMAND_DETECTOR_ENABLED=true to enable)")
```

Then add `command_detector=command_detector, command_memory_writer=command_memory_writer,` to the `await serve(...)` call's keyword arguments.

> Verify the exact names in `run_gateway.py` before finalizing: `agent_config` (load_agent_config result), `dispatcher`, `llm_chat`, `capability_provider` (ReportedCapabilityProvider), `speaker_registry`, `atom_store`, `SystemClock` (imported at top), `UuidIdGenerator`, `StrictCommandValidator`. All confirmed present as of this plan revision.

- [ ] **Step 2: Manual smoke check**

Start the gateway with the detector off (default) and confirm no behavior change:

```
OPENRECALL_ASR_BACKEND=parakeet python scripts/run_gateway.py
```

Then enable it and confirm it logs `command detector enabled` at startup without crashing:

```
OPENRECALL_COMMAND_DETECTOR_ENABLED=true OPENRECALL_SPEAKER_ENABLED=true python scripts/run_gateway.py
```

Expected: startup log includes `command detector enabled (require_wearer=True)`; no exceptions; audio/transcript flow unchanged. (Ctrl+C to stop each.)

- [ ] **Step 3: Commit**

```bash
git add scripts/run_gateway.py src/openrecall_server/gateway/adapter.py
git commit -m "feat(gateway): wire CommandDetector + CommandMemoryWriter in run_gateway + serve"
```

---

### Task 9: Docs — env vars + spec/plan cross-link

**Files:**
- Modify: `scripts/run_gateway.py` header comment (the env-var index near the top) and/or the project README env section.

- [ ] **Step 1: Document the new env vars**

Add the `OPENRECALL_COMMAND_*` vars to the env-var index at the top of `scripts/run_gateway.py` (the existing `OPENRECALL_*` listing), with a one-line description each (enabled, require_wearer, phrases, confidence_threshold, cooldown_s, max_inflight, llm overrides). Note the off-by-default + confirmed-wearer-gate behavior.

- [ ] **Step 2: Commit**

```bash
git add scripts/run_gateway.py
git commit -m "docs(gateway): document OPENRECALL_COMMAND_* env vars"
```

---

## Self-Review

**Spec coverage:**
- Two-stage (keyword + LLM) → Tasks 2–5. ✓
- Not rigid (scoped LLM outputs structured command) → Task 4 prompt/parser. ✓
- Memory for every executed command → Task 6, on ack. ✓
- Day-one photo/video/audio → `DEFAULT_COMMAND_PHRASES` (Task 1). ✓
- Confirmed-wearer gate → Task 3 `feed` + Task 1 config. ✓
- Immediate (per-sentence, not 60s window) → Task 7 `_emit` hook. ✓
- No Planner/no retrieval → Task 5 `_dispatch`. ✓
- Contention defense (cooldown + in-flight cap) → Task 3. ✓
- Off by default → Task 1 config + Task 8 gate. ✓
- Config-list phrases → Task 1 `OPENRECALL_COMMAND_PHRASES` + `DEFAULT_COMMAND_PHRASES`. ✓
- Extensibility (reminders later) → stated as localized change in spec; not implemented here (out of scope). ✓
- Android speaker-labelling follow-up → spec Follow-up section; out of scope here. ✓

**Placeholder scan:** Task 7 Step 1 test body is described in prose ("mirror the existing harness") rather than full code — this is deliberate (the implementer must read the existing gateway fixture to match its fakes), but it is the one place a full code block would be ideal. The Task 7 Step 3 note flags the two facts to verify (`_command_type` return, `trigger_text` recovery). Acceptable given the existing-test dependency; the implementer resolves them against `core.py`.

**Type consistency:**
- `CommandDetectorConfig` fields used in Tasks 3/5/8 match Task 1. ✓
- `Stage2Result` (Task 4) consumed in Task 5. ✓
- `IssueCommandPayload(command_type, params, idempotency_key, confidence)` matches `contracts/types.py:79`. ✓
- `MemoryAtom(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, source_pipeline_version)` matches `memory/atom.py`. ✓
- `atom_id = command_id` idempotency (Task 6) ↔ `_provenance[command_id] = source_event_id` (Task 5). ✓
- `_provenance` read in `_on_command_ack` (Task 7) ↔ written in `_dispatch` (Task 5). ✓

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-25-speech-command-channel.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**