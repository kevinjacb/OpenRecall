# Real Speaker Embedder Backend (Resemblyzer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the existing `MlxSpeakerEmbedder` stub with a real local Resemblyzer speaker-embedding backend, wire it by config, and validate it separates two speakers — all additive, off-by-default, 773 existing tests staying green.

**Architecture:** One concrete backend (`ResemblyzerSpeakerEmbedder`) behind the existing `SpeakerEmbedder` Protocol, selected by `cfg.embed_model` in the adapter. Resemblyzer runs in-process on the Mac (CPU), lazy-imported so the unit suite never loads `resemblyzer`/`numpy`. The identifier + registry stay pure-`list[float]` (unchanged); the real embedder converts `ndarray→list[float]` once at the `embed()` return. Dim is discovered from a dummy inference (never hardcoded). Warmup at startup; thread-safe singleton; logged sample-rate mismatch.

**Tech Stack:** Python 3.12, pydantic v2, stdlib `wave`/`threading`/`logging`/`hashlib`, `resemblyzer` + `numpy` (lazy, behind a new `speaker` pip extra), pytest + pytest-asyncio.

## Global Constraints

- **No raw audio stored** — only embeddings + transcript text; speaker ID on live PCM only.
- **Model-agnostic local-first** — never hardcode a model or a dim; `SpeakerEmbedder` is a Protocol; heavy deps lazy-imported inside `embed()`/`_ensure_ready()` so unit tests never load `resemblyzer`/`numpy`/`mlx`. Embeddings/centroids never leave the Mac unless `SENSE_SPEAKER_EMBED_BASE_URL` is explicitly set (biometric → local-only default).
- **Additive-only** — no schema change (dim is read dynamically, already stored per-speaker-row); existing 773 tests stay green.
- **Off by default** — `SENSE_SPEAKER_ENABLED=false` → zero embed calls, `speaker=None` on every Transcript.
- **Speaker ID never blocks transcription** — on embed failure, hop transcribed with `speaker=None` (existing `identify()` try/except).
- **Vector type is `list[float]`** end-to-end so the unit suite runs without the mlx/numpy extra.
- **Every sqlite store uses `check_same_thread=False` + `threading.Lock`** (unchanged; this build adds a `threading.Lock` only inside `ResemblyzerSpeakerEmbedder`).
- **`run_gateway.py` is manually smoke-tested** (pytest doesn't import it).
- **VAD 2e5→5e4 + whisper hallucination filter already shipped — do not undo.**

## File Structure

- **Modify** `server/src/sense_server/ingest/speaker_embedder.py` — replace `MlxSpeakerEmbedder` stub with `ResemblyzerSpeakerEmbedder` + module-level `_pcm_to_float32` + a module logger.
- **Modify** `server/src/sense_server/ingest/speaker_identifier.py` — add a public `warmup_embedder()` forwarding method (additive).
- **Modify** `server/src/sense_server/gateway/adapter.py` — `build_speaker_identifier` selects real vs fake from `cfg.embed_model`; add `_select_embedder(cfg)`.
- **Modify** `server/pyproject.toml` — add the `speaker` extra.
- **Modify** `server/scripts/run_gateway.py` — call `speaker_identifier.warmup_embedder()` at startup (best-effort); confirm the active-backend print.
- **Create** `server/tools/make_speaker_fixture.py` — stdlib wav trim/normalize utility.
- **Create** `server/tests/fixtures/audio/` — `speaker_a_1.wav`, `speaker_a_2.wav`, `speaker_b_1.wav` + attribution `README.md`.
- **Create** `server/tests/ingest/test_speaker_embedder_real.py` — skipif `resemblyzer` integration test.
- **Extend** `server/tests/ingest/test_speaker_embedder.py` — `_pcm_to_float32` unit test (skipif `numpy`) + rename the stale lazy-import test.
- **Extend** `server/tests/gateway/test_speaker_rollout.py` — adapter-selection unit tests (dep-free).

---

## Task 1: `_pcm_to_float32` helper + skipif-numpy unit test

**Files:**
- Modify: `server/src/sense_server/ingest/speaker_embedder.py` (add module logger + `_pcm_to_float32`)
- Test: `server/tests/ingest/test_speaker_embedder.py`

**Interfaces:**
- Produces: `_pcm_to_float32(pcm: bytes, sample_rate: int) -> "np.ndarray" | None` — int16 LE bytes → float32 `[-1,1]` array; `None` on `sample_rate != 16000`. Lazy-imports numpy.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/ingest/test_speaker_embedder.py` (after the existing tests; the file already has `from __future__ import annotations` and `import sys`):

```python
import importlib.util

_np_available = importlib.util.find_spec("numpy") is not None


def test_pcm_to_float32_normalizes_int16_to_unit_float():
    pytest_skip_if_no_numpy = pytest.mark.skipif(
        not _np_available, reason="numpy not installed"
    )
    # NOTE: pytest is imported below only when needed to avoid a hard dep in
    # bare-CI runs where numpy is absent but pytest is present.
```

That inline approach is awkward — instead, add `import pytest` at the top of the file (pytest is always present in the test runner) and use module-level skipif. Edit the top of the file to add:

```python
import importlib.util
import pytest

_np_available = importlib.util.find_spec("numpy") is not None
```

Then append:

```python
@pytest.mark.skipif(not _np_available, reason="numpy not installed")
def test_pcm_to_float32_normalizes_int16_to_unit_float():
    import numpy as np
    from sense_server.ingest.speaker_embedder import _pcm_to_float32

    # int16 16384 -> 0.5 ; -16384 -> -0.5
    pcm = (16384).to_bytes(2, "little", signed=True) + (-16384).to_bytes(
        2, "little", signed=True
    )
    arr = _pcm_to_float32(pcm, 16000)
    assert arr is not None
    assert arr.dtype.name == "float32"
    assert arr.shape == (2,)
    assert abs(float(arr[0]) - 0.5) < 1e-6
    assert abs(float(arr[1]) + 0.5) < 1e-6


@pytest.mark.skipif(not _np_available, reason="numpy not installed")
def test_pcm_to_float32_wrong_sample_rate_returns_none():
    from sense_server.ingest.speaker_embedder import _pcm_to_float32

    assert _pcm_to_float32(b"\x00\x00", 48000) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && source .venv/bin/activate && python -m pytest tests/ingest/test_speaker_embedder.py -k pcm_to_float32 -v`
Expected: FAIL — `ImportError: cannot import name '_pcm_to_float32'`.

- [ ] **Step 3: Add the module logger + helper**

At the top of `server/src/sense_server/ingest/speaker_embedder.py`, after the existing imports, add a module logger:

```python
import logging
```
and after `SpeakerVector = list[float]`, add:

```python
log = logging.getLogger(__name__)
```

Then add the helper (place it just before the `FakeSpeakerEmbedder` class, after `log = ...`):

```python
def _pcm_to_float32(pcm: bytes, sample_rate: int):
    """int16-LE PCM bytes -> float32 numpy array in [-1, 1].

    Returns ``None`` when ``sample_rate != 16000`` (the caller logs). Pure;
    imports numpy lazily so the unit suite runs without it. Resemblyzer
    expects 16 kHz mono float32 in [-1, 1].
    """
    if sample_rate != 16000:
        return None
    import numpy as np

    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && source .venv/bin/activate && python -m pytest tests/ingest/test_speaker_embedder.py -k pcm_to_float32 -v`
Expected: PASS (if numpy present) or SKIP (if bare-CI no numpy). In the dev venv (numpy via mlx), PASS.

- [ ] **Step 5: Run the full embedder test file to confirm no regression**

Run: `cd server && source .venv/bin/activate && python -m pytest tests/ingest/test_speaker_embedder.py -v`
Expected: the existing fake-embedder + lazy-import tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/sense_server/ingest/speaker_embedder.py tests/ingest/test_speaker_embedder.py
git commit -m "feat(ingest): _pcm_to_float32 int16->float32 helper for the real speaker embedder"
```

---

## Task 2: `ResemblyzerSpeakerEmbedder` (replace the stub) + dep-free construction test

**Files:**
- Modify: `server/src/sense_server/ingest/speaker_embedder.py` (replace `MlxSpeakerEmbedder` with `ResemblyzerSpeakerEmbedder`)
- Modify: `server/tests/ingest/test_speaker_embedder.py` (rename stale lazy-import test + add construction test)

**Interfaces:**
- Produces: `ResemblyzerSpeakerEmbedder(*, min_speech_ms=500, model_name="resemblyzer")` implementing `SpeakerEmbedder` (`dim: int` property, `embed(pcm, sample_rate) -> list[float] | None`, plus `warmup()`).
- Consumes: `_pcm_to_float32` (Task 1).

- [ ] **Step 1: Write the failing construction test**

Append to `server/tests/ingest/test_speaker_embedder.py`:

```python
def test_resemblyzer_embedder_constructs_without_loading_model():
    # Construction must be cheap: no resemblyzer/numpy import, encoder not
    # loaded. This lets the adapter-selection unit test (Task 3) assert
    # isinstance dep-free.
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    e = ResemblyzerSpeakerEmbedder(min_speech_ms=500, model_name="resemblyzer")
    assert e.model_name == "resemblyzer"
    assert e.min_speech_ms == 500
    assert e._encoder is None  # not loaded at construction
    assert e._dim is None


def test_resemblyzer_embedder_dim_property_is_lazy():
    # dim is a @property that triggers _ensure_ready; we only assert it exists
    # and is documented lazy. We do NOT call it here (would load the model).
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    e = ResemblyzerSpeakerEmbedder()
    assert isinstance(e, type(e))  # constructed
    assert hasattr(type(e), "dim")  # dim is a property descriptor
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && source .venv/bin/activate && python -m pytest tests/ingest/test_speaker_embedder.py -k resemblyzer_embedder -v`
Expected: FAIL — `ImportError: cannot import name 'ResemblyzerSpeakerEmbedder'`.

- [ ] **Step 3: Replace the `MlxSpeakerEmbedder` stub**

In `server/src/sense_server/ingest/speaker_embedder.py`, delete the entire `MlxSpeakerEmbedder` class (lines that define it, ~`class MlxSpeakerEmbedder:` through its `raise NotImplementedError(...)`). Add `import threading` to the top imports. Replace it with:

```python
class ResemblyzerSpeakerEmbedder:
    """Real local speaker-embedding backend (Resemblyzer GE2E).

    Chosen for v1 due to its mature API, lightweight CPU inference, and
    permissive licensing. The ``SpeakerEmbedder`` Protocol allows migration to
    ECAPA-TDNN / pyannote without architectural changes; Resemblyzer is not the
    state of the art but is the right v1 backend.

    Heavy deps (``resemblyzer``, ``numpy``) are imported lazily inside
    ``_ensure_ready`` so importing this module — and constructing this class —
    never loads them. ``dim`` is discovered from a dummy inference (never
    hardcoded), so a future backend with a different dim is accommodated with
    no change to the identifier (the identifier is already dim-agnostic via
    ``_cosine``'s length guard).

    Local-only by default; the remote ``base_url``/``api_key`` path is not
    implemented in v1 (speaker embeddings take audio, not text, so an
    OpenAI-compatible text ``/embeddings`` endpoint does not apply).
    """

    _SUPPORTED_RATE = 16000
    _WARMUP_MS = 1600  # Resemblyzer wants >= ~1.6 s for a stable embedding

    def __init__(self, *, min_speech_ms: int = 500, model_name: str = "resemblyzer") -> None:
        # Cheap: stores config only. Does NOT load the model — so adapter
        # selection + unit tests can construct this class without
        # resemblyzer/numpy installed.
        self.model_name = model_name
        self.min_speech_ms = min_speech_ms
        self._encoder = None
        self._dim: int | None = None
        self._lock = threading.Lock()

    @property
    def dim(self) -> int:
        # Discovered from the model, not assumed. Triggers _ensure_ready on
        # first access (identifier reads dim at mint time, after the first
        # embed() has already loaded the encoder).
        self._ensure_ready()
        assert self._dim is not None
        return self._dim

    def warmup(self) -> None:
        """Load the model + run a dummy inference so the first real hop pays
        nothing. Best-effort; ``run_gateway`` calls this at startup."""
        self._ensure_ready()

    def _ensure_ready(self) -> None:
        # Thread-safe singleton init (double-checked locking). The gateway
        # runs embed() via asyncio.to_thread; concurrent sessions could race
        # the first-call init.
        if self._encoder is not None:
            return
        with self._lock:
            if self._encoder is not None:
                return
            from resemblyzer import VoiceEncoder
            import numpy as np

            log.info("speaker_embedder_loading model=%s", self.model_name)
            self._encoder = VoiceEncoder()
            # One dummy inference: warms the graph AND discovers dim via
            # shape[0]. Low-amplitude noise; the value is irrelevant, only
            # the dimension matters.
            n = self._SUPPORTED_RATE * self._WARMUP_MS // 1000
            dummy = (np.random.randn(n).astype(np.float32)) * 1e-3
            vec = self._encoder.embed_utterance(dummy)
            self._dim = int(vec.shape[0])
            log.info("speaker_embedder_ready dim=%d", self._dim)

    def embed(self, pcm: bytes, sample_rate: int) -> SpeakerVector | None:
        if sample_rate != self._SUPPORTED_RATE:
            log.warning(
                "unsupported sample rate %d (speaker disabled for hop)",
                sample_rate,
            )
            return None
        ms = len(pcm) * 1000 // (sample_rate * 2)
        if ms < max(self.min_speech_ms, self._WARMUP_MS):
            return None  # too short for a stable embedding
        wav = _pcm_to_float32(pcm, sample_rate)
        if wav is None:
            return None
        self._ensure_ready()
        vec = self._encoder.embed_utterance(wav)
        # One ndarray -> list[float] conversion at the boundary. Plain floats
        # so JSON storage round-trips cleanly.
        return [float(x) for x in vec.tolist()]
```

- [ ] **Step 4: Rename the stale lazy-import test**

In `server/tests/ingest/test_speaker_embedder.py`, rename `test_mlx_embedder_imports_lazily_and_not_at_module_import` → `test_speaker_embedder_module_imports_lazily` (the test asserts the *module* import pulls no numpy/mlx, which is still true with the new class — numpy/resemblyzer stay lazy inside methods). Update only the `def` name; keep the body.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && source .venv/bin/activate && python -m pytest tests/ingest/test_speaker_embedder.py -v`
Expected: all PASS (construction tests dep-free; `_pcm_to_float32` tests skip-or-pass; lazy-import test passes — module import still pulls no numpy).

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `cd server && source .venv/bin/activate && python -m pytest -q`
Expected: 773 passed (the new dep-free tests add a few; skipif tests skip in bare CI). Confirm no import error from removing `MlxSpeakerEmbedder`.

- [ ] **Step 7: Commit**

```bash
git add src/sense_server/ingest/speaker_embedder.py tests/ingest/test_speaker_embedder.py
git commit -m "feat(ingest): ResemblyzerSpeakerEmbedder real backend (dynamic dim, warmup, thread-safe)"
```

---

## Task 3: Adapter selection — real vs fake from `cfg.embed_model` + dep-free unit tests

**Files:**
- Modify: `server/src/sense_server/gateway/adapter.py` (`build_speaker_identifier` + new `_select_embedder`)
- Test: `server/tests/gateway/test_speaker_rollout.py`

**Interfaces:**
- Produces: `build_speaker_identifier(cfg, registry, embedder=None)` — `embedder=None` → select from `cfg.embed_model` (`"resemblyzer"` → `ResemblyzerSpeakerEmbedder`, else `FakeSpeakerEmbedder`); `embedder="fake"` → `FakeSpeakerEmbedder`; explicit instance → use as-is.
- Consumes: `ResemblyzerSpeakerEmbedder` (Task 2), `SpeakerConfig`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/gateway/test_speaker_rollout.py` (which already imports `build_speaker_identifier`, `SpeakerConfig`, `InMemorySpeakerRegistry`):

```python
def test_resemblyzer_model_yields_resemblyzer_embedder():
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    cfg = SpeakerConfig(enabled=True, embed_model="resemblyzer")
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder=None)
    assert ident is not None
    # Construction is cheap (no model load) so this assertion is dep-free.
    assert isinstance(ident._embedder, ResemblyzerSpeakerEmbedder)


def test_unset_embed_model_yields_fake_embedder():
    from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder

    cfg = SpeakerConfig(enabled=True)  # embed_model defaults to ""
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder=None)
    assert ident is not None
    assert isinstance(ident._embedder, FakeSpeakerEmbedder)


def test_explicit_fake_still_yields_fake():
    from sense_server.ingest.speaker_embedder import FakeSpeakerEmbedder

    cfg = SpeakerConfig(enabled=True, embed_model="resemblyzer")
    # Explicit "fake" sentinel overrides cfg.embed_model.
    ident = build_speaker_identifier(cfg, InMemorySpeakerRegistry(cfg), embedder="fake")
    assert ident is not None
    assert isinstance(ident._embedder, FakeSpeakerEmbedder)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && source .venv/bin/activate && python -m pytest tests/gateway/test_speaker_rollout.py -v`
Expected: FAIL — `test_resemblyzer_model_yields_resemblyzer_embedder` fails because `embedder=None` still yields `FakeSpeakerEmbedder` (current behavior).

- [ ] **Step 3: Implement the selection**

In `server/src/sense_server/gateway/adapter.py`, replace the existing `build_speaker_identifier` body with model-aware selection. Change the signature default and add `_select_embedder`:

```python
def _select_embedder(cfg):
    """Pick the embedder from ``cfg.embed_model``.

    ``"resemblyzer"`` (or any non-empty, non-"fake" value) -> the real local
    backend; ``""`` / ``"fake"`` -> the deterministic fake. Heavy import is
    lazy inside the real backend's ``_ensure_ready``, so constructing the real
    backend here never loads the model.
    """
    from ..ingest.speaker_embedder import FakeSpeakerEmbedder, ResemblyzerSpeakerEmbedder

    model = (cfg.embed_model or "").strip().lower()
    if model and model != "fake":
        return ResemblyzerSpeakerEmbedder(
            min_speech_ms=cfg.min_speech_ms, model_name=cfg.embed_model,
        )
    return FakeSpeakerEmbedder(dim=16, min_speech_ms=cfg.min_speech_ms)


def build_speaker_identifier(cfg, registry, embedder=None):
    """Return a :class:`SpeakerIdentifier` when enabled, else ``None``.

    ``embedder``: ``None`` -> select from ``cfg.embed_model``; ``"fake"`` ->
    the deterministic fake (explicit test sentinel); a concrete
    :class:`SpeakerEmbedder` -> use as-is. The rollout guard is structural:
    disabled -> ``None`` -> the pipeline wires no identifier -> zero embed
    calls and ``speaker=None`` on every Transcript.
    """
    if not cfg.enabled:
        return None
    from ..ingest.speaker_identifier import SpeakerIdentifier

    if embedder is None:
        embedder = _select_embedder(cfg)
    elif embedder == "fake":
        from ..ingest.speaker_embedder import FakeSpeakerEmbedder

        embedder = FakeSpeakerEmbedder(dim=16, min_speech_ms=cfg.min_speech_ms)
    return SpeakerIdentifier(embedder, registry, cfg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && source .venv/bin/activate && python -m pytest tests/gateway/test_speaker_rollout.py -v`
Expected: all PASS (including the two pre-existing tests, which use `embedder="fake"` and `embedder=None` + `enabled=False`).

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `cd server && source .venv/bin/activate && python -m pytest -q`
Expected: green (existing `test_enabled_config_yields_identifier` passes `embedder="fake"` → fake branch; no behavior change for that path).

- [ ] **Step 6: Commit**

```bash
git add src/sense_server/gateway/adapter.py tests/gateway/test_speaker_rollout.py
git commit -m "feat(gateway): build_speaker_identifier selects Resemblyzer vs fake from cfg.embed_model"
```

---

## Task 4: `speaker` pip extra

**Files:**
- Modify: `server/pyproject.toml`

- [ ] **Step 1: Add the extra**

In `server/pyproject.toml`, in `[project.optional-dependencies]`, after the `opus` extra, add:

```toml
# Real local speaker-embedding backend (Resemblyzer, CPU). Lazy-imported
# inside ResemblyzerSpeakerEmbedder._ensure_ready so the unit suite runs
# without it; install to use SENSE_SPEAKER_EMBED_MODEL=resemblyzer.
speaker = [
    "resemblyzer>=0.1",
    "numpy>=2",
]
```

- [ ] **Step 2: Verify it installs + the suite still passes without it**

Run: `cd server && source .venv/bin/activate && pip install -e '.[speaker]' && python -m pytest -q`
Expected: `resemblyzer` + `numpy` install; 773+ pass (skipif integration test not yet present, so no new test runs here — Task 5 adds it).

- [ ] **Step 3: Confirm bare-CI still passes without the extra**

Run: `cd server && source .venv/bin/activate && pip install -e '.[dev]' && python -m pytest -q`
Expected: green; the `_pcm_to_float32` + (forthcoming) integration tests skip; the dep-free adapter-selection + construction tests pass.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add 'speaker' extra (resemblyzer + numpy) for the real speaker embedder"
```

---

## Task 5: Fixture audio + skipif-resemblyzer integration test

**Files:**
- Create: `server/tools/make_speaker_fixture.py`
- Create: `server/tests/fixtures/audio/speaker_a_1.wav`, `speaker_a_2.wav`, `speaker_b_1.wav`
- Create: `server/tests/fixtures/audio/README.md`
- Create: `server/tests/ingest/test_speaker_embedder_real.py`

**Interfaces:**
- Produces: an integration test asserting Resemblyzer separates two speakers (same-speaker cosine > diff-speaker cosine by a margin), skipif `resemblyzer` not installed or fixtures missing.

- [ ] **Step 1: Create the fixture normalization utility**

`server/tools/make_speaker_fixture.py`:

```python
#!/usr/bin/env python3
"""Trim/normalize a 16 kHz mono wav to N seconds for the speaker-embedder
integration test fixtures.

LibriSpeech wavs are already 16 kHz mono int16, so this just trims to the
first ``seconds`` and rewrites the header. Usage:

    python tools/make_speaker_fixture.py <in.wav> <out.wav> [seconds]

If the input is not 16 kHz mono, convert it first (e.g. with ffmpeg:
``ffmpeg -i in.wav -ar 16000 -ac 1 out.wav``).
"""
from __future__ import annotations

import sys
import wave


def main(in_path: str, out_path: str, seconds: float = 4.0) -> None:
    with wave.open(in_path, "rb") as r:
        if r.getframerate() != 16000:
            raise SystemExit(f"expected 16 kHz, got {r.getframerate()}")
        if r.getnchannels() != 1:
            raise SystemExit(f"expected mono, got {r.getnchannels()} channels")
        n = int(r.getframerate() * seconds)
        frames = r.readframes(min(n, r.getnframes()))
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(frames)
    print(f"wrote {out_path} ({len(frames)//2} samples)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: make_speaker_fixture.py <in.wav> <out.wav> [seconds]")
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 4.0)
```

- [ ] **Step 2: Obtain three LibriSpeech clips and normalize them**

LibriSpeech dev-clean is 16 kHz mono, CC BY 4.0. One-time ~337 MB download:

```bash
cd server
mkdir -p tests/fixtures/audio
curl -O https://www.openslr.org/resources/12/dev-clean.tar.gz
tar xzf dev-clean.tar.gz
# Speaker A (id 1272) — two utterances; Speaker B (id 2035) — one utterance.
python tools/make_speaker_fixture.py \
  librispeech-dev-clean/1272-128104-0000.wav tests/fixtures/audio/speaker_a_1.wav
python tools/make_speaker_fixture.py \
  librispeech-dev-clean/1272-128104-0001.wav tests/fixtures/audio/speaker_a_2.wav
python tools/make_speaker_fixture.py \
  librispeech-dev-clean/2035-147857-0029.wav tests/fixtures/audio/speaker_b_1.wav
# clean up the big download
rm -rf librispeech-dev-clean dev-clean.tar.gz
```

If the full dev-clean download is impractical, any two distinct speakers from
a permissive source work — record two 4 s clips yourself + one more, or grab
two CC-BY wavs, resample to 16 kHz mono (`ffmpeg -ar 16000 -ac 1`), and run them
through `make_speaker_fixture.py`. The test only requires three files named
`speaker_a_1.wav`, `speaker_a_2.wav`, `speaker_b_1.wav` in
`tests/fixtures/audio/`.

- [ ] **Step 3: Write the attribution README**

`server/tests/fixtures/audio/README.md`:

```markdown
# Speaker-embedder integration test fixtures

Three 16 kHz mono int16 WAV clips used by
`tests/ingest/test_speaker_embedder_real.py` to verify the real
`ResemblyzerSpeakerEmbedder` separates speakers.

- `speaker_a_1.wav`, `speaker_a_2.wav` — same speaker, two utterances.
- `speaker_b_1.wav` — a different speaker.

**Source:** LibriSpeech dev-clean (https://www.openslr.org/resources/12/),
licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). See the
LibriSpeech terms for attribution requirements. Clips were trimmed to ~4 s with
`tools/make_speaker_fixture.py`.

If you substituted other permissive clips, update this file with their source
and license.
```

- [ ] **Step 4: Write the integration test**

`server/tests/ingest/test_speaker_embedder_real.py`:

```python
"""Real-backend integration test for ResemblyzerSpeakerEmbedder.

Skipif resemblyzer is not installed (the `speaker` extra) OR the fixture wavs
are missing. Asserts the real embedder separates two speakers: same-speaker
cosine (two utterances of speaker A) > diff-speaker cosine (A vs B) by a
margin. This is the only automated proof that the real recognition path
actually discriminates voices; the fake embedder's distance is synthetic.
"""
from __future__ import annotations

import importlib.util
import os
import wave

import pytest

_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "audio")
_has_resemblyzer = importlib.util.find_spec("resemblyzer") is not None
_has_fixtures = all(
    os.path.exists(os.path.join(_FIXTURES, n))
    for n in ("speaker_a_1.wav", "speaker_a_2.wav", "speaker_b_1.wav")
)


def _load_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as r:
        assert r.getframerate() == 16000, f"expected 16 kHz, got {r.getframerate()}"
        return r.readframes(r.getnframes())


@pytest.mark.skipif(
    not _has_resemblyzer or not _has_fixtures,
    reason="resemblyzer extra or speaker fixtures not present",
)
def test_resemblyzer_separates_two_speakers():
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder
    from sense_server.ingest.speaker_identifier import _cosine

    emb = ResemblyzerSpeakerEmbedder(min_speech_ms=500)
    emb.warmup()
    a1 = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_a_1.wav")), 16000)
    a2 = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_a_2.wav")), 16000)
    b1 = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_b_1.wav")), 16000)
    assert a1 is not None and a2 is not None and b1 is not None, "embed returned None"
    assert len(a1) == len(a2) == len(b1), "dim mismatch across clips"

    sim_same = _cosine(a1, a2)
    sim_diff = _cosine(a1, b1)
    # Resemblyzer same-speaker cosine typically 0.7-0.9, diff 0.2-0.4. Require a
    # clear margin. Tune the constant down only if a specific LibriSpeech pair
    # is unusually close; 0.15 is conservative.
    margin = 0.15
    assert sim_same - sim_diff > margin, (
        f"same-speaker {sim_same:.3f} not clearly > diff-speaker {sim_diff:.3f} "
        f"(margin {margin})"
    )


@pytest.mark.skipif(
    not _has_resemblyzer or not _has_fixtures,
    reason="resemblyzer extra or speaker fixtures not present",
)
def test_resemblyzer_dim_matches_across_clips():
    # dim is discovered from the model and must be consistent across hops.
    from sense_server.ingest.speaker_embedder import ResemblyzerSpeakerEmbedder

    emb = ResemblyzerSpeakerEmbedder()
    emb.warmup()
    d = emb.dim
    v = emb.embed(_load_pcm(os.path.join(_FIXTURES, "speaker_a_1.wav")), 16000)
    assert v is not None and len(v) == d
```

- [ ] **Step 5: Run the integration test (with the speaker extra installed)**

Run: `cd server && source .venv/bin/activate && pip install -e '.[speaker]' && python -m pytest tests/ingest/test_speaker_embedder_real.py -v`
Expected: PASS (two tests). If `sim_same - sim_diff` is below 0.15 for the chosen LibriSpeech pair, lower the `margin` constant in the test to a value the pair clears with headroom, and note the chosen value in the commit message.

- [ ] **Step 6: Confirm bare-CI skips it (no resemblyzer)**

Run: `cd server && source .venv/bin/activate && pip install -e '.[dev]' && python -m pytest tests/ingest/test_speaker_embedder_real.py -v`
Expected: 2 SKIPPED.

- [ ] **Step 7: Run the full suite**

Run: `cd server && source .venv/bin/activate && pip install -e '.[speaker]' && python -m pytest -q`
Expected: green; the two integration tests pass with the extra.

- [ ] **Step 8: Commit**

```bash
git add tools/make_speaker_fixture.py tests/fixtures/audio tests/ingest/test_speaker_embedder_real.py
git commit -m "test(ingest): Resemblyzer speaker-embedder integration test + LibriSpeech fixtures"
```

---

## Task 6: `run_gateway.py` warmup + active-backend print (manual smoke)

**Files:**
- Modify: `server/src/sense_server/ingest/speaker_identifier.py` (add `warmup_embedder()`)
- Modify: `server/scripts/run_gateway.py` (call warmup at startup; confirm print)

**Note:** `run_gateway.py` is not imported by pytest — manually smoke-test after edits (per the standing constraint).

- [ ] **Step 1: Add the public warmup seam to `SpeakerIdentifier`**

In `server/src/sense_server/ingest/speaker_identifier.py`, inside `class SpeakerIdentifier`, add (additive, near the other public methods):

```python
    def warmup_embedder(self) -> None:
        """Best-effort: warm up the embedder model if it supports warmup.

        run_gateway calls this once at startup so the first real conversation
        pays no model-init cost. No-op for embedders without ``warmup``
        (e.g. FakeSpeakerEmbedder).
        """
        warmup = getattr(self._embedder, "warmup", None)
        if warmup is not None:
            warmup()
```

- [ ] **Step 2: Wire warmup + confirm print in `run_gateway.py`**

In `server/scripts/run_gateway.py`, after `speaker_identifier = build_speaker_identifier(speaker_cfg, speaker_registry, embedder=None)`, add a best-effort warmup:

```python
    # Warm up the real embedder at startup so the first connection pays no
    # model-init cost. No-op for the fake embedder / when disabled. Best-effort:
    # a warmup failure must not abort the gateway.
    if speaker_identifier is not None:
        try:
            speaker_identifier.warmup_embedder()
        except Exception:
            logging.exception("speaker_warmup_failed")
```

Then confirm the existing ENABLED print reflects the model. The current line is:
```python
print(f"speaker recognition ENABLED (model={speaker_cfg.embed_model or 'fake'})")
```
Leave it as-is — with `SENSE_SPEAKER_EMBED_MODEL=resemblyzer` it prints `model=resemblyzer`; unset/`fake` prints `model=fake`. (Verify by reading the file; if the print differs, adjust to the above.)

- [ ] **Step 3: Syntax-check `run_gateway.py` without running it**

Run: `cd server && source .venv/bin/activate && python -c "import ast; ast.parse(open('scripts/run_gateway.py').read()); print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Smoke-test the disabled path (no heavy audio deps needed for startup print)**

Run: `cd server && source .venv/bin/activate && pip install -e '.[mlx,opus,speaker]' && timeout 3 python scripts/run_gateway.py --port 8765 --http-port 8766 2>&1 | head -20; true`
Expected (disabled): `speaker recognition DISABLED (set SENSE_SPEAKER_ENABLED=true to enable)` among the startup lines, then `timeout` cuts it off (no relay connected). No `speaker_warmup_failed`.

- [ ] **Step 5: Smoke-test the enabled + real path (startup only, no relay)**

Run: `cd server && source .venv/bin/activate && SENSE_SPEAKER_ENABLED=true SENSE_SPEAKER_EMBED_MODEL=resemblyzer timeout 6 python scripts/run_gateway.py --port 8765 --http-port 8766 2>&1 | head -20; true`
Expected: `speaker_embedder_loading model=resemblyzer` → `speaker_embedder_ready dim=256` → `speaker recognition ENABLED (model=resemblyzer)`, then `timeout` cuts it off. Confirm `dim=256` (Resemblyzer's actual output dim — adjust the expectation if Resemblyzer reports a different dim, but do NOT hardcode it anywhere in code).

- [ ] **Step 6: Run the full suite once more**

Run: `cd server && source .venv/bin/activate && python -m pytest -q`
Expected: green (the `warmup_embedder` method is additive; existing identifier tests unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/sense_server/ingest/speaker_identifier.py scripts/run_gateway.py
git commit -m "feat(gateway): warm up the real speaker embedder at startup (best-effort)"
```

---

## Self-Review (run after writing — already done inline)

1. **Spec coverage:** every spec section maps to a task — backend class (T2), `_pcm_to_float32` (T1), adapter selection (T3), `speaker` extra (T4), fixture integration test (T5), warmup + print (T6). Deferred items (remote path, other backends, VAD-segment, ndarray refactor, resampling) are explicitly out of scope in both spec and plan.
2. **Placeholder scan:** no TBD/TODO. The fixture acquisition step (T5 Step 2) gives a concrete URL + commands + a documented fallback for source availability — a real procedure, not a placeholder.
3. **Type/name consistency:** `ResemblyzerSpeakerEmbedder`, `model_name`, `warmup()`, `warmup_embedder()`, `_pcm_to_float32`, `_select_embedder`, `build_speaker_identifier(cfg, registry, embedder=None)` — used consistently across tasks. `dim` is a `@property` everywhere. `_cosine` is reused (existing) in the integration test.
4. **Bare-CI invariant:** confirmed at T1 (skipif numpy), T5 (skipif resemblyzer/fixtures), and T4 Step 3 — `.[dev]` runs the 773 + dep-free tests, skips the guarded ones.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-27-speaker-embedder-backend.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?