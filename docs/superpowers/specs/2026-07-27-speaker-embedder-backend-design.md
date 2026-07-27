# Speaker Embedder Backend (Resemblyzer) — Design

**Date:** 2026-07-27
**Status:** Approved (design)
**Supersedes / fills:** the `MlxSpeakerEmbedder` stub in
`server/src/sense_server/ingest/speaker_embedder.py` (which raises
`NotImplementedError` and whose docstring defers the inference wiring to the
hardware bring-up).

## Goal

Fill the existing `MlxSpeakerEmbedder` stub with a real local speaker-embedding
model (Resemblyzer) so `SENSE_SPEAKER_ENABLED=true` performs **actual** speaker
recognition, validated by an automated fixture test. The seam
(`SpeakerEmbedder` Protocol), config (`SpeakerConfig`), rollout guard, and
lazy-import discipline already exist — this is "implement the backend behind
the existing stub and wire it by config", not a new subsystem.

## Architecture

One concrete backend (`ResemblyzerSpeakerEmbedder`) behind the existing
`SpeakerEmbedder` Protocol. Resemblyzer runs in-process on the Mac (CPU),
lazy-imported inside `embed()` so importing the module — and running the unit
suite — never loads `resemblyzer`/`numpy`. The adapter selects the backend
from `cfg.embed_model` at wiring time: `"resemblyzer"` → real, `"fake"` or
unset → `FakeSpeakerEmbedder` (unchanged). The identifier and registry are
**unchanged** — they stay pure-`list[float]` (no numpy in the hot path), and
the real embedder converts `ndarray→list[float]` exactly once at the
`embed()` return boundary.

> **Why Resemblyzer for v1.** Chosen for its mature API, lightweight CPU
> inference, and permissive licensing. The `SpeakerEmbedder` Protocol allows
> migration to ECAPA-TDNN / pyannote without architectural changes. Resemblyzer
> is not the state of the art (it ranks behind ECAPA-TDNN, pyannote embeddings,
> and SpeechBrain ECAPA) but is the right v1 backend: lightweight, stable, easy
> to integrate, well documented.

## Components

### `ResemblyzerSpeakerEmbedder` (replaces the `MlxSpeakerEmbedder` stub)

Honest name — Resemblyzer is not MLX. The `SpeakerEmbedder` Protocol is
unchanged. `SpeakerVector = list[float]` is unchanged (see "Why list[float]"
below).

```
class ResemblyzerSpeakerEmbedder:
    dim: int                      # @property — discovered from the model, not hardcoded
    model_name: str               # stamped on speaker rows as embedding_model
    min_speech_ms: int             # floor; effective floor is max(this, 1600)

    __init__(*, min_speech_ms, model_name="resemblyzer")
        # stores config only — does NOT load the model (keeps construction cheap
        # so the adapter-selection unit test can build the class without numpy)

    @property
    def dim(self) -> int
        # calls _ensure_ready(); returns self._dim (discovered from a dummy inference)

    def embed(self, pcm: bytes, sample_rate: int) -> list[float] | None
        # 1. if sample_rate != 16000: log.warning(...); return None
        # 2. ms = len(pcm)*1000 // (sample_rate*2); if ms < max(min_speech_ms,1600): return None
        # 3. _ensure_ready()   # lazy load + warmup + discover dim, lock-guarded
        # 4. wav = _pcm_to_float32(pcm)            # int16 LE bytes -> float32 [-1,1]
        # 5. vec_np = self._encoder.embed_utterance(wav)   # 256-dim np.ndarray
        # 6. return vec_np.tolist()                          # one ndarray->list conversion
        #    (any failure -> let it raise; identify() catches it -> speaker=None)

    def warmup(self) -> None
        # public; run_gateway calls it once at startup so the first real
        # conversation pays no model-init cost. Just calls _ensure_ready().

    def _ensure_ready(self) -> None
        # threading.Lock-guarded (double-checked locking on self._encoder is None):
        #   - lazy import resemblyzer.VoiceEncoder + numpy
        #   - self._encoder = VoiceEncoder()
        #   - dummy inference on a ~1.6s synthetic wav to warm the graph AND
        #     discover self._dim = dummy_vec.shape[0]
        # idempotent after first call
```

**Key design rules:**
- **No hardcoded dim.** `dim` is discovered from `dummy_vec.shape[0]` during
  `_ensure_ready()`, never assumed. A future ECAPA backend (dim 192) or a
  512-dim backend is accommodated with no change to the identifier (the
  identifier is already dim-agnostic — see "Cross-model safety").
- **`__init__` is cheap.** It stores config only. The model loads lazily on
  first `embed()`, first `dim` access, or explicit `warmup()` — so the
  adapter-selection unit test can construct the class without `numpy`
  installed.
- **Warmup is explicit + at startup.** `run_gateway` calls `embedder.warmup()`
  once after building the real embedder (best-effort, logged). The first real
  conversation pays nothing.
- **Thread-safe singleton.** `_ensure_ready()` is guarded by a
  `threading.Lock` (the gateway runs `embed()` via `asyncio.to_thread`;
  concurrent sessions could race first-call init). Double-checked locking on
  `self._encoder is None`.
- **Sample-rate mismatch is logged, not silent.** `if sample_rate != 16000:
  log.warning("unsupported sample rate %d (speaker disabled for hop)",
  sample_rate); return None`. A 48 kHz feed doesn't silently disable
  recognition.
- **Effective min-speech floor is `max(min_speech_ms, 1600)`.** Resemblyzer
  warns below ~1.6 s; sub-1.6 s fragments return `None` ("no speech in hop",
  same semantics as today). The 5 s hop comfortably clears this.

### `_pcm_to_float32(pcm: bytes, sample_rate: int) -> "np.ndarray" | None`

Pure, unit-testable **without** the model (it only needs `numpy`, which is in
the `speaker` extra — but the unit test for it can use a tiny numpy stub or be
skipif-guarded; see Testing). Responsibilities:

- `if sample_rate != 16000: return None` (the caller logs; the helper just
  bails — keeps it pure and independently testable).
- bytes (int16 LE) → `numpy.frombuffer(pcm, dtype=numpy.int16)` →
  `astype(numpy.float32) / 32768.0` → normalized `[-1, 1]` array.
- Return the array (caller passes to `embed_utterance`).

Resemblyzer's `embed_utteration` internally calls `preprocess_wav` (silence
trim via optional `webrtcvad`; works without it). We feed the normalized array
directly; Resemblyzer handles trimming.

### Adapter selection — `build_speaker_identifier`

`build_speaker_identifier(cfg, registry, embedder)` gains model-driven
selection when no explicit `embedder` is passed:

```
if not cfg.enabled: return None
if embedder is None:
    if cfg.embed_model and cfg.embed_model != "fake":
        embedder = ResemblyzerSpeakerEmbedder(
            min_speech_ms=cfg.min_speech_ms, model_name=cfg.embed_model,
        )
    else:
        embedder = FakeSpeakerEmbedder(dim=16, min_speech_ms=cfg.min_speech_ms)
# else: explicit embedder (tests / "fake") — use as-is
return SpeakerIdentifier(embedder, registry, cfg)
```

Explicit-`embedder` override is preserved so the existing test path
(`embedder="fake"`) and any test passing a real/fake instance are unchanged.

### `pyproject.toml` — `speaker` extra

```
speaker = [
    "resemblyzer>=0.1",
    "numpy>=2",
]
```

Lazy-imported inside `embed()` / `_ensure_ready()`, so the bare
`pip install -e '.[dev]'` unit suite never needs them.

### `scripts/run_gateway.py` — warmup + active-backend print

After building `speaker_identifier`, if it is not `None` and the selected
embedder is the real backend, call `embedder.warmup()` (best-effort,
`try/except` + log). The existing `speaker recognition ENABLED (model=…)`
print already reflects `cfg.embed_model or 'fake'`; ensure it prints
`resemblyzer` when the real backend is selected so the operator can confirm
which path is live.

## Data flow

hop PCM (5 s, 16 kHz, int16 LE) → `ResemblyzerSpeakerEmbedder.embed` →
(log+None on wrong rate) → (None if <1.6 s) → `_ensure_ready` (lazy load +
warmup + dim discovery, once) → `_pcm_to_float32` →
`VoiceEncoder.embed_utterance` → 256-dim `np.ndarray` → `.tolist()` (one
conversion) → `SpeakerIdentifier.identify` → `_match` (cosine vs `list[float]`
centroids; pure-Python, no numpy) → existing confirm/tentative/cluster/"You"
path unchanged → `SpeakerAssignment` or `None`.

`identify()` wraps `embed()` in `try/except` (already present): on any
failure (model not installed, bad PCM, Resemblyzer internal error) it logs
`speaker_embed_failed` and returns `None` → hop transcribed with
`speaker=None`. **Speaker ID never blocks transcription.**

## Why `list[float]` end-to-end (the point-5 decision)

The identifier and registry stay pure-`list[float]`; the real embedder converts
`ndarray→list` exactly once at the `embed()` return, never back. This was
chosen over holding `np.ndarray` internally because:

- **No thrash either way.** The "list→numpy→list→numpy inside cosine" thrash
  only happens in a *mixed* state. The current all-`list` design has zero
  numpy in the hot path (`_cosine` is a pure-Python loop on lists) — one
  `ndarray→list` conversion per hop at the boundary, none back. Thrash is
  avoided by not being mixed, and we are already not mixed.
- **The identifier is already dim-agnostic.** `_cosine` returns `0.0` on
  length mismatch (`speaker_identifier.py:51`); no dim is hardcoded in the
  identifier. "SpeakerIdentifier unaware of dimensionality" is already true.
- **Backend swap is already trivial.** Swap = change `SENSE_SPEAKER_EMBED_MODEL`
  + wipe `speakers.db`. Cross-dim is safe by the len-guard. A future ndarray
  refactor would not make this more trivial.
- **Honors the standing constraint.** Memory: "Vector type is `list[float]`
  (not numpy) so the whole unit suite runs without the mlx extra." Going
  ndarray internally would require numpy in the speaker unit tests, relaxing
  that invariant, for a perf gain (numpy cosine) that's negligible at hop rate
  (a few hops/sec × 256-dim dot product = microseconds — not a bottleneck).

If hop-rate cosine ever profiles as hot, the ndarray refactor is a clean
future optimization; the `SpeakerEmbedder` Protocol already abstracts the
boundary so the identifier need not change.

## Cross-model safety (already safe — documented only)

`_cosine` returns `0.0` when `len(a) != len(b)` (`speaker_identifier.py:51`).
A 256-dim Resemblyzer vector against a 16-dim centroid (from a prior
`FakeSpeakerEmbedder` run, or a future 192-dim ECAPA run) scores `0.0` →
never matches → falls to clustering → mints fresh. Changing
`SENSE_SPEAKER_EMBED_MODEL` therefore **orphans** old speakers (they score 0
forever, never match) rather than corrupting recognition. **Operator
guidance:** to re-enroll cleanly after switching embedder model, wipe
`speakers.db`. No code change required for safety.

## Config / rollout (off-by-default unchanged)

| Env | Result |
|---|---|
| `SENSE_SPEAKER_ENABLED` unset/`false` | no identifier wired (structural guard, unchanged) |
| `=true`, `SENSE_SPEAKER_EMBED_MODEL` unset/`fake` | `FakeSpeakerEmbedder` (wiring-only; current behavior) |
| `=true`, `SENSE_SPEAKER_EMBED_MODEL=resemblyzer` | `ResemblyzerSpeakerEmbedder` (real recognition) |

`SENSE_SPEAKER_EMBED_BASE_URL` / `_API_KEY` are reserved, unused in v1
(`SpeakerConfig` already has the fields). The remote HTTP path is deferred —
speaker embeddings take audio, not text, so an "OpenAI-compatible" text
`/embeddings` endpoint does not apply; a remote speaker backend would need a
custom audio protocol and is out of scope.

## Error handling

- `resemblyzer`/`numpy` not installed → lazy import raises inside
  `embed()`/`_ensure_ready()` → caught by `identify()` → `speaker=None`,
  logged `speaker_embed_failed`. The log message points the operator to
  `pip install -e '.[speaker]'`.
- Wrong sample rate → logged warning + `None` (not silent).
- Sub-1.6 s / too-quiet hop → `None` ("no speech in hop").
- Bad PCM / Resemblyzer internal error → caught → `None`.
- All failures best-effort; transcription never blocked (existing
  `identify()` try/except).

## Testing

- **Unit, no guard (bare `.[dev]` CI, no `resemblyzer`/`numpy` needed):**
  - Adapter selection: `build_speaker_identifier(cfg(enabled=True,
    embed_model="resemblyzer"), InMemorySpeakerRegistry(cfg), embedder=None)`
    returns a `SpeakerIdentifier` whose embedder is a `ResemblyzerSpeakerEmbedder`
    instance — assertable by `isinstance` **without** loading the model.
    Construction is cheap (`__init__` stores config only; no numpy/resemblyzer
    import), and the module imports only stdlib at import time, so this test
    runs dep-free. And `embed_model="fake"` / unset → `FakeSpeakerEmbedder`
    (unchanged behavior).
  - Existing 773 tests unchanged (the `FakeSpeakerEmbedder` path).
- **Unit, skipif `numpy` not installed:**
  - `_pcm_to_float32`: synthetic `int16` bytes — correct `dtype`/shape, `[-1, 1]`
    normalization (a known int16 value maps to the expected float), and `None`
    on `sample_rate != 16000`. `@pytest.mark.skipif(not
    importlib.util.find_spec("numpy"))`. The helper's sample-rate branch
    returns `None` so this test exercises only the pure conversion, not the
    model.
- **Integration, skipif `resemblyzer` not installed:**
  - `@pytest.mark.skipif(not importlib.util.find_spec("resemblyzer"))`
  - Load two LibriSpeech clips (CC BY 4.0, attributed in
    `tests/fixtures/audio/README`) — two distinct speakers, one clip each
    (or two per speaker for a same-speaker pair).
  - Embed each via `ResemblyzerSpeakerEmbedder.embed`.
  - Assert **same-speaker cosine > diff-speaker cosine** by a margin
    (e.g. `sim_same - sim_diff > 0.15`, tuned to Resemblyzer's typical
    separation; the exact margin is fixed empirically in the plan).
  - CI without the `speaker` extra still passes (skipif).

**Bare-CI property:** `pip install -e '.[dev]' && pytest` runs the 773
existing tests + the adapter-selection test (no guards) and **skips** the
`_pcm_to_float32` test (no numpy) and the integration test (no resemblyzer).
The dev venv (which already has `numpy` via the `mlx` extra) runs the
`_pcm_to_float32` test; installing the `speaker` extra runs the integration
test. The no-numpy unit-suite invariant is preserved.
- **Integration (skipif `resemblyzer` not installed):**
  - `@pytest.mark.skipif(not importlib.util.find_spec("resemblyzer"))`
  - Load two LibriSpeech clips (CC BY 4.0, attributed in
    `tests/fixtures/audio/README`) — two distinct speakers, one clip each
    (or two per speaker for a same-speaker pair).
  - Embed each via `ResemblyzerSpeakerEmbedder.embed`.
  - Assert **same-speaker cosine > diff-speaker cosine** by a margin
    (e.g. `sim_same - sim_diff > 0.15`, tuned to Resemblyzer's typical
    separation; the spec's exact margin is fixed empirically in the plan).
  - CI without the `speaker` extra still passes (skipif).
- **Fixtures:** two ~3–5 s 16 kHz mono `.wav` clips in
  `tests/fixtures/audio/` + a `README` citing LibriSpeech (CC BY 4.0) as the
  source. Loaded via `wave` (stdlib) → raw PCM → `embed()`.

## Files

- Modify `server/src/sense_server/ingest/speaker_embedder.py` — replace the
  `MlxSpeakerEmbedder` stub with `ResemblyzerSpeakerEmbedder` +
  `_pcm_to_float32` + a module logger.
- Modify `server/src/sense_server/gateway/adapter.py` —
  `build_speaker_identifier` selects real vs fake from `cfg.embed_model`.
- Modify `server/pyproject.toml` — add the `speaker` extra.
- Modify `server/scripts/run_gateway.py` — call `embedder.warmup()` at startup
  (best-effort) when the real backend is selected; confirm the active-backend
  print.
- Create `server/tests/fixtures/audio/` — two `.wav` clips + attribution
  `README`.
- Create `server/tests/ingest/test_speaker_embedder_real.py` — the skipif
  integration test.
- Extend `server/tests/ingest/test_speaker_embedder.py` — `_pcm_to_float32`
  unit test + adapter-selection unit test (or place the adapter-selection test
  in `tests/gateway/test_speaker_rollout.py` alongside the existing rollout
  tests).

## Out of scope (deferred)

- Remote `SENSE_SPEAKER_EMBED_BASE_URL` path (audio, not text → not
  OpenAI-compatible; custom protocol; biometric → local-only default).
- SpeechBrain ECAPA-TDNN / pyannote / ONNX-via-onnxruntime backends
  (documented upgrade paths; the Protocol accommodates them without
  architectural change).
- VAD-driven speech-segment embedding (the real fix for the 5 s mixed-speaker
  window). v1 embeds the 5 s hop as-is; mixed-window damage is contained by
  corroboration-before-mint + no-poison-on-tentative (existing). This is a
  separate roadmap item.
- Audio resampling (16 kHz assumed — the uplink is fixed 16 kHz; a non-16 kHz
  feed logs and returns `None`).
- `np.ndarray`-internal refactor of the identifier/registry (see "Why
  `list[float]`"). A future optimization if hop-rate cosine ever profiles hot.

## Standing constraints preserved (do not undo)

- No raw audio stored — only embeddings + transcript text; speaker ID on live
  PCM only.
- Model-agnostic local-first; embeddings/centroids never leave the Mac unless
  `SENSE_SPEAKER_EMBED_BASE_URL` explicitly set. Heavy deps lazy-imported so
  unit tests never load `resemblyzer`/`numpy`/`mlx`.
- Additive-only — no schema change (dim is read dynamically, already stored
  per-speaker-row); existing tests stay green.
- Off-by-default (`SENSE_SPEAKER_ENABLED=false` → zero embed calls,
  `speaker=None` on every Transcript).
- Speaker ID never blocks transcription (`identify()` try/except → `None`).
- Events/atoms store the stable `speaker_id` UUID, never a display name.
- Vector type is `list[float]` so the unit suite runs without the mlx extra.
- Every sqlite store: `check_same_thread=False` + `threading.Lock`
  (unchanged; this build adds a `threading.Lock` only inside
  `ResemblyzerSpeakerEmbedder` for the singleton encoder).
- `run_gateway.py` manually smoke-tested (pytest doesn't import it).
- VAD 2e5→5e4 + whisper hallucination filter already shipped — do not undo.