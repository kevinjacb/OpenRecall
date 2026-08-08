# Findings — design vs. server endpoint gap analysis

**Date:** 2026-08-08
**Design source:** Claude Design project `OpenRecall` (`c9d85fba-6033-48a3-ba50-3571bdbad26f`), file `Sense Relay.dc.html`
**Server source:** `server/src/openrecall_server/http/` at commit `d9d39ab`

The design is a single interactive Android prototype rendering **7 pages**:
Pairing, Home, Recordings, Recording Detail, Memories, Chat, Settings.

> The design file still uses pre-rebrand naming throughout ("Sense Relay", "Hey Sense").
> Worth a pass before it becomes the build spec.

---

## Current server surface (16 endpoints)

| Method | Path |
|---|---|
| GET | `/health` |
| GET | `/provisioning/pubkey` |
| GET | `/status` |
| GET | `/metrics` |
| GET | `/sessions` (`?limit`, `?cursor`) |
| GET | `/sessions/{id}` |
| GET | `/sessions/{id}/events` |
| GET | `/sessions/{id}/memory` |
| GET | `/memory` (`?q` **required**, `?session_id`, `?limit`) |
| POST | `/agent` |
| GET | `/speakers` |
| POST | `/speakers/reassign` |
| POST | `/speakers/{id}/rename` |
| GET | `/commands` |
| GET | `/commands/{id}` |
| POST | `/commands/{id}/ack` |

---

## Page-by-page coverage

### 1. Pairing (scan → server config) — ✅ covered
BLE scan list is phone-local. The URL/token form validates against `GET /health`,
which already returns `gatewayPort` for WS URL derivation.

### 2. Home — partial
Covered: version + "863 events today" from `GET /status` (`version`,
`recentEvents24h`); "42 ms" round-trip is client-measured; the live "Hearing now"
line arrives over the §E WebSocket `transcript` frame; recent-session rows from
`GET /sessions` (`preview`, `durationMs`, `transcriptCount`, `startedAt`).

Gaps: **battery %**, **session title**.

### 3. Recordings (list + search) — partial
Grouping into Today/Yesterday/Earlier is client-side from `startedAt`. ✅

Gaps: **transcript search**, **session title**, **per-session memory count badge**.

### 4. Recording Detail — partial
Covered: transcript segments from `GET /sessions/{id}/events` (text, `startMs`,
`speakerName`, `isWearer`); "Memories from this session" chips from
`GET /sessions/{id}/memory`.

Gaps: **the entire audio player** (play/pause, waveform, playhead), **title**,
**the `⋮` overflow menu actions**.

### 5. Memories (browse + kind filter) — ❌ essentially uncovered
The worst-served page. See gaps #1 and #2.

### 6. Chat — ✅ covered
`POST /agent` handles sends and returns `answer` / `atoms` / `outcome`
(including `refuse`). Proactive "Noticed for you" bubbles arrive over the §E WS
`proactive` frame.

Minor: chat history is phone-local (`ChatHistoryStore`) with no server
persistence — lost on reinstall, not shared across devices. The design does not
require otherwise, so this is a note, not a gap.

### 7. Settings — partial
Covered: "Events (24 h)" from `/status`; URL/token/address are phone-local.

Gaps: **firmware version**, **battery**, and **all three capture toggles**.

---

## Missing endpoints

### Blocking — the page cannot function

| # | Endpoint | Why |
|---|---|---|
| 1 | `GET /memory` **without `q`**, plus `?kind=&limit=&cursor=` | The Memories tab lists and filters by kind (All / Tasks / People / Decisions / Places / Preferences). Today `/memory` returns **400 if `q` is empty** and is semantic-search-only — no kind filter, no listing, no pagination. There is no way to render this page. |
| 2 | `GET /memory/stats` | Design header reads "128 memories, 6 added today". No total count exists; `returned_count` reflects only what a given search returned. |
| 3 | `GET /sessions/{id}/audio` (with Range support) | The detail page has play/pause, scrubbing, and a duration readout. **No audio is retrievable at all** — `_event_to_wire` hardcodes `codec:""`, `sampleRateHz:0`, `byteCount:0`, with an inline comment that the audio plane is "Phase 5+". `BlobStore` exists in `media/blob.py` but has no HTTP route, and audio is never written to it. |
| 4 | `GET /sessions/{id}/waveform` | The 34-bar waveform. Either serve precomputed peaks or accept that the client must download the full audio to draw it. |
| 5 | `POST /commands` | Settings toggles map onto `start_audio` / `stop_audio`. Commands can currently **only** be created by the agent's internal `issue_command` path — the HTTP surface exposes list / get / ack, with no create route. |

### Significant — page renders but degraded

| # | Endpoint / field | Why |
|---|---|---|
| 6 | `title` on `SessionSummary` (+ `PATCH /sessions/{id}` to rename) | Used on Home, Recordings, the detail header, and inside every memory's `source` line ("Studio standup · 01:04"). `SessionSummary` (`sessions/index.py:49`) has no title field — only `preview`. Needs LLM-generated titling. |
| 7 | `GET /sessions?q=` | The "Search transcripts" input. `/sessions` accepts only `limit`/`cursor`. `/memory?q` searches *atoms* and returns atoms — the wrong object type for this list. |
| 8 | `memoryCount` on `SessionSummary` | The "4 memories" badge on every row. Currently requires N+1 calls to `/sessions/{id}/memory`. |
| 9 | `GET` / `PUT /settings` | Nothing persists the three capture toggles; they would be phone-local and silently diverge from actual server behavior. |
| 10 | `GET /device/status` | Battery %, firmware version, storage, recording state. `DeviceResourceStatus` (with `battery_pct`) exists at `contracts/types.py:290` but is fed by `ConstantCapabilityProvider` — a stub with hardcoded values, never exposed over HTTP. Making this real also requires firmware and §C.6 support, not just a route. |
| 11 | `DELETE /sessions/{id}` | The detail page's `⋮` menu implies delete / export. |
| 12 | `DELETE /devices/{id}` (or a data-purge route) | "Forget this Sense" — unclear whether it should drop server-side data or only unpair locally. |

---

## Conflicts that are not endpoint gaps

**Wake word.** The Settings toggle "Wake word — Only listen after 'Hey Sense'"
contradicts the architecture: the README states the wearable has *no wake word*
by design, and the firmware contains no wake-word engine. Either cut the toggle,
or redefine it as server-side gating after transcription — a different feature
that should be labeled honestly.

**Two existing capabilities the design drops.**

- *Speaker naming.* The transcript shows a bare "Speaker 2" with no way to name
  them, while `/speakers/{id}/rename` and `/speakers/reassign` exist and the
  current Android app already has a name-the-speaker flow.
- *Commands screen.* `/commands` exists and the current Android app has a
  Commands UI; the design has no equivalent.

Decide whether these were deliberate cuts or oversights before the design
becomes the build target.

---

## Summary

**5 blocking, 7 significant.** The Memories tab and the audio player are the two
features with essentially zero server support; everything else is additive fields
on shapes that already exist.

Suggested build order: audio persistence + retrieval is the long pole (it touches
firmware, ingest, and storage — not just a route), so start there. Session
titling and the memory list/filter endpoints are comparatively cheap and unblock
three pages between them.
