# Speaker Recognition — Design Spec

**Date:** 2026-07-26
**Status:** Draft v2 (revised from v1 after expert review; awaiting user review)
**Scope:** Sub-project 1 of 3 — speaker recognition / voiceprint. Naming +
continuous-learning UX is folded in here (including a manual-correction loop).
Song/music detection is a separate future spec. All three share the "run a model
on live PCM in the ingest pipeline" seam but are otherwise independent.

## Revision note (v1 → v2)

v1 updated centroids on every accepted match and minted a new unknown on every
failed match — the two classic failure modes of online speaker recognition
(centroid poisoning and registry bloat), and it conflated recognition with
clustering. v2 separates the two, requires corroboration before minting, guards
centroids with a ring buffer + outlier trim + EMA, propagates confidence, and
adds a manual-correction loop. These changes were driven by review feedback.

## Goal

Attribute every captured utterance to a speaker, with a confidence and an
assignment type. The wearer is "You"; everyone else starts as a corroborated
"Unknown" and is named via an in-chat nudge. The system learns continuously but
**adaptively** (EMA + bounded ring buffer, not a running mean over all time), and
mistakes are correctable via a manual path that turns errors into training signal.
This gives the memory layer "who said what" and lets the proactive/agent layer
reason about conversations ("Sarah asked me to…").

## Hard constraints (from the existing codebase)

Non-negotiable; shape every decision:

1. **No raw audio is ever stored.** Only transcript text + timing on events
   (`events/model.py:20-30`). Speaker ID must run on the **live PCM** as it flows
   through ingest (`ingest/pipeline.py`, `ingest/whisper_streaming.py`). It cannot
   be retroactive.
2. **Audio is single-channel mono 16 kHz.** Speakers cannot be separated by
   channel; separation is by **voice fingerprint** (an embedding model on the
   PCM), not routing.
3. **No entity/person model exists yet.** A voiceprint→named-person registry is a
   new table; the event/atom schemas have no `speaker` field today (clean
   additive attachment points exist).
4. **Whisper has no diarization** in this pipeline. Transcription emits a single
   text blob per 1 s hop (`ingest/streaming_transcriber.py`). Speaker ID is a
   separate, parallel stage, not inside Whisper.
5. **Model-agnostic, local-first.** Project rule: never hardcode a model; pluggable
   via config, local or cloud. Voiceprints are biometric → default is local-only
   on the Mac gateway (alongside mlx-whisper + Ollama).
6. **The firmware already emits per-frame VAD state** (C6_SPEECH /
   C6_GAP_MARKER / PREROLL / HANGOVER in `config.h`). This is the foundation for
   the roadmap item: VAD-driven speech-segment embedding instead of fixed windows.

## Architecture & data flow — recognition and clustering separated

Speaker ID is two separable stages that the v1 draft had intertwined. Splitting
them lets clustering improve independently of recognition.

```
BLE → relay → gateway core.on_audio
  → AudioIngestPipeline.ingest (pipeline.py:97)
      → reassembler → Opus decode → PCM
      → [existing] StreamingTranscriber.feed → Transcript(text, duration_ms)
      → [NEW] SpeakerIdentifier.identify(pcm, sr)
            1. Embed     → SpeakerEmbedder.embed(pcm, sr) → vec | None
            2. RECOGNITION — cosine vs confirmed/named centroids only
                 ≥ confirm_threshold      → confirmed assignment (high confidence)
                 [tentative, confirm)     → tentative assignment → assignment buffer
                 < tentative_threshold     → fall through to clustering
            3. CLUSTERING — unmatched embeddings → pending scratch clusters
                 N embeddings within X s that cluster together AND match nothing
                   → mint a persistent "unknown_N" registry row
                 no recurrence within TTL → garbage-collect (no permanent row)
      → Transcript gains speaker{id, confidence, assignment}
  → core._emit writes CaptureEvent(..., speaker, speaker_confidence, speaker_assignment)
  → EventStore + SessionIndex + ExtractionEnqueuer + TranscriptMsg (carries speaker)
  → ExtractionStage windows events → atoms inherit speaker{...}
```

`SpeakerIdentifier` holds the live registry + pending clusters in memory and
persists confirmed speakers to the `SpeakerRegistry` store. Speaker ID can fail
independently of transcription (on failure, the hop is transcribed and stored
with `speaker=None`; ASR never blocks on it).

## Data model

Additive changes following existing patterns. Events and atoms store the **stable
`speaker_id` (UUID)**, never a display name; display names live only on the
registry row and are resolved by a join at read time (renaming updates one row,
not every event).

1. **`CaptureEvent` gains three nullable fields** (`events/model.py:20-30`) +
   SQLite columns via idempotent `ALTER TABLE`s mirroring `memory/migrations.py`'s
   `ADDITIONS`:
   - `speaker` (TEXT, UUID) — the assigned speaker id, or NULL (silence/no-speech hop).
   - `speaker_confidence` (REAL, 0..1) — cosine of the match that assigned it.
   - `speaker_assignment` (TEXT) — `confirmed` | `tentative` | `none`.

2. **`MemoryAtom` gains the same three fields** (`atom.py:21-35`) + migration
   columns. For a 60 s extraction window with mixed speakers: take the majority
   speaker by turn-weighted confidence; if no majority, `speaker=None`. The
   extraction prompt is also told per-line speakers (resolved to display names) so
   the LLM can attribute facts. **The memory/agent layer can ignore weak
   assignments** (`assignment="tentative"` or `confidence < floor`) — this is the
   point of propagating confidence.

3. **New `SpeakerRegistry` store** (new module, mirroring `EventStore`/`AtomStore`):

   | table | columns | purpose |
   |---|---|---|
   | `speakers` | `speaker_id` (PK, UUID), `display_name` (nullable), `is_wearer`, `enrollment_status` (`implicit`/`confirmed`), `centroid_embedding` (blob, EMA), `embedding_model`, `dim`, `turn_count`, `first_seen`, `updated_at` | one row per corroborated/confirmed speaker |
   | `speaker_embeddings` | `speaker_id` FK, `embedding` (blob), `confidence`, `created_at` | **ring buffer** of the last N (~100) confirmed embeddings per speaker; trimmed of outliers; centroid = EMA recomputed from this buffer |

   No raw audio and no per-turn audio is stored — only embeddings. Pending
   (uncorroborated) scratch clusters are **in-memory with a TTL**, not persisted:
   they become a `speakers` row only after corroboration, so a one-off voice never
   gets a permanent identity.

## Recognition, clustering & hybrid enrollment

Conservatively designed to avoid the two v1 failure modes: never poison a centroid
on a single match, and never mint an identity on a single failed match.

### Recognition stage (match against confirmed/named speakers only)

Per hop, after the energy pre-filter (`min_speech_ms` of voiced audio in the
window, else `None` — also starves the hallucination path):

1. **Embed** → `vec | None` (None when the window is too short/quiet).
2. **Cosine vs every confirmed/named centroid**:
   - **≥ `confirm_threshold`** (high confidence) → `confirmed` assignment. This is
     the **only** path that adds to the speaker's ring buffer / centroid.
   - **`[tentative_threshold, confirm_threshold)`** → `tentative` assignment (event
     is still labeled so the memory is attributable). The embedding is held in a
     short **assignment buffer** for that candidate; only after N consecutive
     tentative hits **agree** is it promoted → confirmed → and only then folded
     into the ring buffer. **A single tentative match never touches a centroid.**
     This is the hysteresis that prevents flip-flop between similar voices and
     prevents a 0.71-above-0.70 near-miss from poisoning Sarah with John's voice.
   - **< `tentative_threshold`** → fall through to clustering.

### Centroid update (bounded, adaptive, outlier-trimmed)

Centroids are **not** running means over all time (that drifts when the mic
moves, the speaker has a cold, or the room changes). Instead:

- Each confirmed speaker keeps a **ring buffer of the last N confirmed embeddings**
  (`speaker_embeddings`, N≈100, configurable).
- **Outlier trim**: drop the farthest ~10% from the buffer before recomputing.
- **Centroid = EMA**: `centroid = α·new + (1-α)·centroid`, α≈0.05 (configurable),
  recomputed from the trimmed buffer. Old behavior falls off naturally → adapts
  to mic/cold/room changes instead of averaging everything forever.

### Clustering stage (corroborate before minting)

Unmatched embeddings go to **pending scratch clusters** (in-memory, TTL'd):

- A new persistent "unknown_N" identity is minted **only when N embeddings within
  X seconds cluster together** (pairwise cosine ≥ `cluster_threshold`) **and**
  none match an existing confirmed/unknown centroid. This is the corroboration
  gate — a TV voice, a delivery guy, a podcast each produce one stray embedding
  that never corroborates and is garbage-collected. No registry bloat.
- Pending clusters with no recurrence within `pending_ttl_s` are dropped (no
  `speakers` row ever written for them).

### "You" enrollment (hybrid)

Cold registry: first voiced hops' unmatched embeddings sit as a pending cluster;
once it corroborates it becomes `unknown_1`. Dominant-voice heuristic — mic is on
the wearer, so the cluster with the most turns in the first `coldstart_window_s`
is tagged `is_wearer=True, display_name="You", enrollment_status="implicit"`. Not
confirmed yet. Once it accrues enough confirmed turns → fire the confirm nudge →
`enrollment_status="confirmed"`.

**Safeguard:** if a second cluster's turn count approaches the wearer's within the
cold-start window, **do not auto-pick** — hold both as unnamed and nudge the user
to say which is "you." Avoids the "loud non-wearer at boot" failure.

**Thresholds are config-driven** (`SENSE_SPEAKER_*`): the right cosine gaps are
model-dependent (pyannote vs ECAPA vs speechbrain differ), and the project is
model-agnostic.

## Manual correction path (first-class v1)

Review feedback flagged this as one of the highest-value additions: without it,
mistakes become permanent. With it, every correction improves future recognition.

- **Phone UI:** each utterance shows its speaker; a "wrong speaker?" affordance
  lets the user pick the right one (existing named speaker, "new person", or
  "me").
- **`ReassignSpeaker` control message** over the existing relay WS:
  `{from_speaker_id, to_speaker_id, scope}` where `scope` is one utterance, a time
  range, or all of a speaker's turns (chosen by the user).
- **Server action** (`SpeakerRegistry.reassign`):
  1. Re-label all matching `CaptureEvent` + `MemoryAtom` rows (`speaker` →
     `to_speaker_id`; bump `speaker_assignment`/`confidence` appropriately).
  2. Move those turns' embeddings **out of** `from`'s ring buffer **into** `to`'s
     (creating `to` if it's a new person).
  3. Recompute both centroids (ring buffer + outlier trim + EMA).
  4. The removed embeddings being outliers in `from`'s buffer naturally cleans
     the poisoned centroid — correction undoes prior mis-attribution.
- This closes the loop: a wrong "Sarah" that was actually Mike, corrected once,
  fixes the events **and** de-poisons Sarah's centroid **and** strengthens Mike's.

## Nudge + naming UX

Reuses the existing proactive path; adds two inbound control messages
(`NameSpeaker`, `ReassignSpeaker`).

**`SpeakerNudgeListener`** — a new listener on `ExtractionWorker`
(`worker.add_listener`, `extraction_worker.py:302-316`) alongside
`proactive_engine.on_session_completion`, running after each `process_session`:
- **Confirm nudge:** implicit "you" crosses `confirm_turns` → `ProactiveMessage`
  "I've been hearing one main voice — is that you?" (yes/no).
- **Name nudge:** a corroborated unknown crosses `name_nudge_turns` and is unnamed →
  `ProactiveMessage` "I noticed you've spoken with the same person a few times —
  want to name them?" (softer, less robotic than "Unknown Speaker 4"). Carries
  the `speaker_id` and 2-3 transcript lines attributed to them (text only, no
  audio) for context. A new optional `propose: {kind: "name_speaker",
  speaker_id}` lets the phone render a quick name input.

Both go through the existing `ProactiveOutbox` → `ProactiveMessage`
(`core.py:61-142`, `protocol/messages.py:98-113`).

**Naming reply** — new §E control message `NameSpeaker{speaker_id, name}` over
the relay WS → `SpeakerRegistry.name`. Keeps one transport; no HTTP endpoint.

**Politeness:** gated by existing `SENSE_RATE_LIMIT_PER_MIN`; nudges deduped per
`speaker_id` (once per unknown, not every session).

## Model seam & configuration (model-agnostic, local-first)

New `SpeakerEmbedder` Protocol (new module
`server/src/sense_server/ingest/speaker_embedder.py`, mirroring
`memory/embeddings.py:18-22`):

```python
class SpeakerEmbedder(Protocol):
    dim: int
    def embed(self, pcm: bytes, sample_rate: int) -> np.ndarray | None: ...
        # None when the window is too short / too quiet for a usable vector
```

**Default `MlxSpeakerEmbedder`** — an mlx/ONNX voice-embedding model (ECAPA-TDNN /
pyannote-style) on the Apple-Silicon Mac, alongside mlx-whisper + Ollama. Heavy
deps imported lazily inside `embed()` so unit tests never load them (same pattern
as `whisper_streaming.py`). A fake/in-memory impl for tests.

**Config** (`run_gateway.py` + env, mirroring `SENSE_EMBED_*` / `SENSE_LLM_*`):
- `SENSE_SPEAKER_ENABLED` — kill-switch (see Rollout).
- `SENSE_SPEAKER_EMBED_MODEL` — local model id (default in `run_gateway.py`).
- `SENSE_SPEAKER_EMBED_{BASE_URL,API_KEY}` — future cloud provider; off by default.
- Matching: `SENSE_SPEAKER_CONFIRM_THRESHOLD` (~0.7),
  `SENSE_SPEAKER_TENTATIVE_THRESHOLD` (~0.55), `SENSE_SPEAKER_MIN_SPEECH_MS`.
- Clustering: `SENSE_SPEAKER_CLUSTER_THRESHOLD`,
  `SENSE_SPEAKER_CORROBORATE_N` (e.g. 3), `SENSE_SPEAKER_CORROBORATE_WINDOW_S`,
  `SENSE_SPEAKER_PENDING_TTL_S`.
- Centroid: `SENSE_SPEAKER_RING_BUFFER_N` (~100),
  `SENSE_SPEAKER_OUTLIER_TRIM_PCT` (~0.1), `SENSE_SPEAKER_EMA_ALPHA` (~0.05).
- Enrollment: `SENSE_SPEAKER_COLDSTART_WINDOW_S`,
  `SENSE_SPEAKER_CONFIRM_TURNS`, `SENSE_SPEAKER_NAME_NUDGE_TURNS` (~8).
- Confidence floor (for the memory layer to ignore weak assignments):
  `SENSE_SPEAKER_MIN_CONFIDENCE`.

**Privacy / local-first:** embeddings and centroids never leave the Mac by
default — no cloud call unless the operator explicitly sets
`SENSE_SPEAKER_EMBED_BASE_URL`. The registry lives in the on-device `data/` dir.

## Edge cases & error handling

- **5 s window mixes two speakers** (v1 known limitation): a window with
  You+Sarah+You yields a degraded embedding. v1 accepts this because the
  corroboration gate and no-poison-on-tentative **contain** the damage — one bad
  embedding can't mint a cluster or move a centroid. The real fix
  (VAD-driven speech-segment embedding) is on the roadmap. Most 5 s windows in a
  2-person conversation are predominantly one speaker, so this is rare.
- **No speech in hop** → `speaker=None`; never embed, never mint.
- **Reconnect / session resume** → registry is **per-device, cross-session**
  (persisted). `speaker_id`s are stable UUIDs; "Sarah" yesterday is "Sarah" today.
- **Cold start with a loud non-wearer at boot** → safeguard: hold + nudge.
- **Embedding-model swap** → store `embedding_model` + `dim` on `speakers`; if the
  configured model's `dim` differs, invalidate all centroids/ring buffers (re-seed
  from nothing; they re-learn). Same invalidation pattern as `EXTRACTOR_VERSION`.
- **Identifier failure** (embed throws) → hop still transcribed with `speaker=None`;
  speaker ID never blocks transcription.
- **Two people with similar voices merge into one cluster** → the name nudge shows
  2-3 sample lines so the user can catch it; the manual correction path ("split" —
  reassign one utterance to a new person) is the v1 remedy. A dedicated
  auto-split-clustering action is out of scope for v1.
- **Confidence floor** → memory/agent layer ignores `assignment="tentative"` and
  `confidence < SENSE_SPEAKER_MIN_CONFIDENCE`, treating those as unattributed.

## Testing strategy (TDD)

- **Unit** `tests/ingest/test_speaker_identifier.py` (fake `SpeakerEmbedder`,
  no mlx):
  - recognition: confirmed match, tentative band + promotion-after-N,
    no-poison-on-single-tentative (assert centroid unchanged after one tentative).
  - clustering: corroboration minting (N within Xs → identity), pending TTL GC
    (one stray → no identity), no-mint-when-matches-existing.
  - centroid: ring-buffer trim + EMA recomputation; adaptation (drift over
    successive embeddings); outlier removal.
  - enrollment: dominant-voice "you" tagging; cold-start two-cluster hold.
  - no-label-on-silence; identifier-failure → `speaker=None`, transcription unblocked.
- **Unit** `tests/ingest/test_speaker_embedder.py`: lazy-import +
  None-on-short-window contract (mirrors `test_whisper_streaming_filters.py`).
- **Schema/migration** `tests/events/test_store.py` + `tests/memory/test_atom_store.py`:
  round-trip the three new columns; idempotent re-migration.
- **Correction** `tests/.../test_speaker_reassign.py`: reassign re-labels events +
  atoms, moves embeddings between ring buffers, recomputes both centroids, cleans
  the poisoned one.
- **Nudge** `tests/.../test_speaker_nudge_listener.py`: confirm + name nudges fire
  at the right turn counts, dedupe per `speaker_id`, respect rate limit, never
  fire when disabled.
- **End-to-end ingest** `tests/gateway/test_core*.py`: scripted embedder wired
  through `AudioIngestPipeline` → `CaptureEvent.speaker{...}` populated and
  threaded into `TranscriptMsg`.
- **Rollout guard**: with `SENSE_SPEAKER_ENABLED=false`, events get
  `speaker=None` and zero embed calls; existing 695 tests stay green.

## Rollout

Off by default, opt-in. Ship behind `SENSE_SPEAKER_ENABLED=false`. Land the
additive schema migrations (safe), get tests green, then the operator flips the
flag on the gateway to gather real embeddings and tune thresholds against the
actual voice before it ever nudges. Keeps biometric/accuracy risk out of the
default path until validated on real hardware.

## Roadmap (not in v1, but planned)

- **VAD-driven speech-segment embedding** (the real fix for the mixed-speaker
  window): segment the live PCM by the firmware's per-frame VAD state (already
  emitted — constraint 6), embed each contiguous speech segment, then ASR. This
  makes each embedding single-speaker by construction and removes the 5 s
  mixed-window limitation. The `SpeakerIdentifier` interface
  (`identify(pcm, sr)`) is unchanged; only how the window is derived changes, so
  this is an internal swap, not a re-architecture.
- **Auto-split-clustering**: detect a single identity that is actually two and
  split it (beyond the manual correction path).
- **Song/music detection**: separate spec; shares the live-PCM seam.