# Redesign handoff — branch `taslim`

Redesign of the Android relay app against the comp in `design/`, plus the
launch-screen change (Home first, not the connect wizard).

**State: builds green, 318 unit tests pass, verified running on an emulator.
Nothing is committed yet — the whole redesign is uncommitted working-tree
changes on branch `taslim`.**

```
git branch --show-current   # taslim
./gradlew :app:assembleDebug :app:testDebugUnitTest --offline   # BUILD SUCCESSFUL
```

---

## The design source

`design/` (untracked — it is not in git, so **copy it across with the working
tree**, or the comp is unavailable on the other machine):

| File | What it is |
|---|---|
| `Sense Relay.dc.html` | **The authoritative comp.** Markup for every screen plus a `<script type="text/x-dc">` block holding the state model, sample data and all colour/spacing decisions. Read this first. |
| `sense-device.js` | three.js render of the locket. Not portable to Android — see the DeviceVisual placeholder below. |
| `android-frame.jsx` | Device-frame chrome for the comp preview. Not part of the app design. |
| `uploads/*.jpeg` | Screenshots of the **old** app (pre-redesign), for before/after only. |
| `screenshots/hero.png` | A crop of the comp's Home screen. |

---

## What changed

### 1. Design tokens (`core/ui/`)

- **`Color.kt`** — rewritten. Private `Palette` of raw hexes from the comp;
  public `SenseColors` semantic token set (`canvas`, `card`, `border`, `ink`,
  `accent`, `accentSoft`, `ok`, `danger`, …) with `light()` / `dark()`
  variants; exposed via `LocalSenseColors`.
- **`OpenSapienTheme.kt`** — provides *both* an M3 `colorScheme` (so stock M3
  widgets look right) and `SenseColors` via `CompositionLocalProvider`.
  Read tokens as **`SenseTheme.colors.<token>`**.
- **`Type.kt`** — real type scale from the comp (SemiBold structural weights,
  negative tracking on headings). Slot-to-usage mapping is in the KDoc.
- **`Shape.kt`** — comp radii, plus `PillShape` / `CardShape` / `TileShape` /
  `FieldShape` / `ButtonShape`.

### 2. New design-system components (`ui/design/`)

`SenseCard`, `SenseButtons` (Primary/Secondary/Danger), `SenseHeaders`
(`SenseAppHeader`, `SenseScreenHeader`, `SenseDetailHeader`,
`SenseGroupLabel`), `StatusBadge` + `StatusTone` + `StatusDot`, `SenseChips`
(`AccentChip`, `FilterPill`), `SenseTextField` + `SenseSearchField`,
`DeviceVisual`, `StatTile` + `TileMeter` + `TileSegments`, `AudioVisuals`
(`LiveBars`, `WaveformStrip`), `SettingsGroup` + rows + switch,
`PlaceholderMark` (`PlaceholderTag`, `PlaceholderNote`), `SenseIcons`.

Restyled in place (signatures kept so older screens still compile):
`EmptyState`, `SectionHeader`, `InfoRow`, `MetricCard`, `StatePill`,
`ConnectionBadge`, `SenseTopBar`. **Deleted:** `AnimatedConnectionDot.kt`
(superseded by `StatusDot`).

### 3. Navigation — 5 tabs

Bottom bar is now **Home · Recordings · Memories · Chat · Settings**, matching
the comp. Memories was previously reachable only via a chat refusal.

Device and Commands lost their Home drill-down cards and are now pushed from
**Settings → Diagnostics**, each with a back arrow.

`BottomBarTest`'s INV-13 ceiling was **raised from 4 to 5** and rewritten to
assert against the real `mainDestinations` list instead of a hand-copied one
(the old test passed vacuously).

### 4. Launch screen — Home, not the wizard

- `AndroidManifest.xml`: **`MainActivity` is now `LAUNCHER`**; `SetupActivity`
  is `exported="false"` and only ever started for-result from MainActivity.
- `MainActivity.startRelayIfProvisioned()` — the wizard used to be the only
  thing that started `RelayService`; now a cold start does it whenever a
  device is provisioned.
- `SetupActivity.EXTRA_RECONFIGURE` **removed** — the wizard always
  `setResult(RESULT_OK)` + `finish()` now.
- Home shows **"Not connected"** + a "Set up your …" CTA when
  `Config.provisioned` is false. `HomeViewModel` gained a
  `ConfigurationRepository` param to combine that flag in.

### 5. Screens rewritten

Home, Recordings, Memories, Chat (+ all bubbles via a new shared
`ChatBubble`), Settings, SessionDetail, the Setup wizard, AtomDetail.

### 6. New behaviour (with tests)

| Behaviour | Where | Test |
|---|---|---|
| Recordings search box | `RecordingsViewModel.onQueryChange` | `RecordingsViewModelTest` |
| Today/Yesterday/Earlier grouping | `RecordingsScreen.groupByRecency` | `SessionGroupingTest` (new) |
| Memories kind filters | `MemoryViewModel.onFilterSelected`, `MemoryState.visibleAtoms` | `MemoryRouteTest` |
| Search-as-you-type | `MemoryViewModel.onQueryChanged` | `MemoryRouteTest` |
| "Forget this device" | `SettingsViewModel.forgetDevice` | `SettingsViewModelTest` |
| Session memories on detail | `SessionDetailViewModel.memories` | — |
| Provisioned flag on Home | `HomeViewModel` | `HomeViewModelTest` |

### 7. App theme (`app/src/main/res/` — new, untracked)

`values/themes.xml`, `values-night/themes.xml`, `values/colors.xml` +
`android:theme="@style/Theme.OpenSapien"` on `<application>`.

**Why:** with no app theme the platform default drew a dark, titled action bar
above every Compose screen. `NoActionBar` removes it; `windowBackground` is
set to the canvas colour so cold start doesn't flash.

---

## Placeholders (design has it, app has no data source)

All are marked in-code with `PlaceholderTag` / `PlaceholderNote` — so
`grep -rn "PlaceholderTag\|PlaceholderNote\|\*\*Placeholder" app/src/main`
lists every one.

| Placeholder | Why | Unblocked by |
|---|---|---|
| **DeviceVisual** (`ui/design/DeviceVisual.kt`) | Comp renders the locket in three.js. Ported as a hand-drawn 2D Canvas keeping silhouette, mic ports, emblem, and — the informative part — LED colour + pulse rate per state. | A real 3D pipeline (Filament/SceneView) + a `.glb` asset. Swap this one composable. |
| **Battery tile** (Home) | No battery characteristic over BLE, no device telemetry in `/status`. Shows `—`. | Firmware battery characteristic. |
| **BLE link bar count** | Connected/disconnected is real; RSSI isn't sampled post-GATT-connect, so bar count is coarse. | Periodic RSSI read in `SensorLink`. |
| **"Hearing now" body** | Elapsed clock + live/idle are real. No live-transcript stream exists (transcripts are per-session over HTTP after the fact), so it falls back to the last session's preview. | A live transcript channel to the UI. |
| **Session audio player** | No audio download/stream endpoint — only transcript events. Transport is disabled; `WaveformStrip` is shaped deterministically from the session id, not real samples. | `GET /sessions/{id}/audio`. |
| **Capture toggles** (Settings) | Firmware exposes no always-on/wake-word mode; no on-device redaction stage. Held in memory only; the screen says so. | Firmware + relay support. |
| **Memory kind filters** | Filter labels are the comp's; the extractor decides real `kind` values and no endpoint lists them. Unmatched filters show empty. | A kinds endpoint, or agreed vocabulary. |
| **AtomDetail provenance** | No `GET /memory/{atomId}`; only search + per-session listing. | That endpoint. |
| **Session titles** | Comp shows human titles ("Studio standup"); server has none. `sessionTitle()` in `ui/recordings/SessionDisplay.kt` returns `Session <first 8 of id>` — **one function to change** if titling lands. | Server-side titling. |
| **Recordings search scope** | Sessions API takes only `limit`/`cursor`, no search. Filter is client-side over paged-in results. | A search param on the sessions endpoint. |

---

## Open items — pick up here

### 1. Finish the brand rename (Sense → OpenSapien) — **not applied**

The comp says "Sense" throughout, but commit `d9d39ab` deliberately rebranded
the repo Sense → OpenSapien, and existing copy already said "your OpenSapien".
I was mid-rename when this paused; **a `cd` in my patch script failed silently,
so none of it landed.** User-facing strings still say "Sense".

```bash
grep -rn "Sense Relay\|your Sense\|this Sense\|\"Sense\"" app/src/main/kotlin/
```

Intended replacements:

| File:line | Current | Intended |
|---|---|---|
| `ui/home/HomeScreen.kt:189` | `SenseAppHeader(title = "Sense Relay"` | `"OpenSapien Relay"` |
| `ui/home/HomeScreen.kt:216` | `label = "Set up your Sense"` | `"Set up your OpenSapien"` |
| `ui/home/HomeScreen.kt:~257` | `"your Sense starts hearing."` | `"your OpenSapien starts hearing."` |
| `ui/home/HomeScreen.kt:~523` | `?: "Sense"` (device-name fallback) | `?: "OpenSapien"` |
| `ui/SetupScreen.kt:68,71` | `"Set up your Sense"` | `"Set up your device"` |
| `ui/SetupScreen.kt:199` | `"Looking for your Sense"` | `"Looking for your OpenSapien"` |
| `ui/SetupScreen.kt:206` | `"Tell your Sense where to send…"` | `"Tell your device where to send…"` |
| `ui/settings/SettingsScreen.kt:206,226` | `"Forget this Sense"` / `"Forget this Sense?"` | `"Forget this device"` / `"…device?"` |
| `ui/settings/SettingsViewModel.kt:55` | KDoc `"Forget this Sense"` | `"Forget this device"` |
| `ui/recordings/RecordingsScreen.kt:110` | `"Sessions from your Sense…"` | `"…your OpenSapien…"` |
| `ui/memory/MemoryScreen.kt:130,174` | `"your Sense chose/has heard"` | `"your OpenSapien …"` |
| `ui/chat/RefuseMessageBubble.kt:29` | `"once your Sense "` | `"once your OpenSapien "` |

Leave alone: internal `Sense*` **type names** (`SenseCard`, `SenseIcons`,
`SenseColors`, `SenseTheme`, …) and comp filenames in KDoc — those are code
identifiers and doc references, not user-facing copy. *(Renaming the `Sense*`
component prefix to `OS*`/`Relay*` is a reasonable follow-up, but it's a
separate mechanical pass — don't mix it into this one.)*

### 2. Re-verify on device — **two fixes are unverified**

The last emulator run was taken **before** these two landed; both compile but
neither has been seen rendering:

- **Icon arc-flag fix** (`ui/design/SenseIcons.kt`). Compose's `PathParser`
  does *not* split SVG's compressed arc flags (`a2 2 0 11-2.9 2.9`), so it read
  `11` as the large-arc flag and produced mangled glyphs — the Settings gear,
  Chat bubble and Signal icons rendered as unrecognisable slivers. Every arc is
  now written as `rx ry rot laf sf x y`. **Keep it that way when adding paths.**
- **App theme** (`res/values/themes.xml`) removing the dark action bar.

```bash
./gradlew :app:assembleDebug --offline
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell monkey -p com.opensapien.relay -c android.intent.category.LAUNCHER 1
# NOTE: this emulator reports multiple displays, so `adb exec-out screencap`
# fails. Use:
adb shell screencap -p /sdcard/s.png && adb pull /sdcard/s.png /tmp/s.png
```

Check: no dark title bar; gear/chat/signal icons legible; then walk all five
tabs, session detail, and the setup wizard.

### 3. Not yet done at all

- **Commit.** Nothing is staged. `design/` is untracked — decide whether it
  belongs in git (it's ~150KB incl. images).
- **Dark theme** is defined but never visually checked.
- **Device / Commands screens** got a back arrow and inherit the new tokens,
  but their internals were not redesigned — they still use the old
  `SectionHeader` + `InfoRow` layout. Acceptable (they're diagnostic), but
  they're the least comp-aligned screens.
- **Chat proactive messages** — `ChatViewModel` has no path that produces
  `AGENT_PROACTIVE`; the bubble exists and is styled but nothing pushes one.
  Pre-existing gap, not introduced here.

---

## Gotchas worth knowing

- **`Modifier.padding` / `PaddingValues` overloads don't mix** `horizontal =`
  with `top =`/`bottom =`. Cost two compile errors; use the 4-arg form.
- **`by animateColorAsState`** needs `import androidx.compose.runtime.getValue`.
- **`org.junit.Assert.assertEquals`** takes the message **first**;
  `kotlin.test.assertEquals` takes it last. Test files here mix both imports.
- No Robolectric / Compose UI tests in this project (standing decision) —
  screens are validated by running them, ViewModels by unit test.
- Build with `--offline`; the toolchain is cached.
