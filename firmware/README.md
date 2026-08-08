# OpenSapien Firmware — Phase 0 Bench Spikes

These are **throwaway measurement sketches**, not product firmware. Their only job
is to turn three estimated numbers into measured facts on your actual
**XIAO ESP32S3 Sense** before we build anything on top of them. Run them, record
the numbers on the scorecard below, and decide go / no-go.

> ⚠️ These sketches were written but **not compiled or flashed by their author**.
> Treat them as drafts to verify on-device. Pin assignments and library APIs are
> the most likely things to need adjustment.

## Setup (Arduino IDE)

1. Boards Manager → install **esp32 by Espressif** (3.x).
2. Tools → Board → **XIAO_ESP32S3**.
3. Tools → **PSRAM → "OPI PSRAM"** (required).
4. Tools → USB CDC On Boot → **Enabled** (so Serial prints work).
5. Serial Monitor @ **115200**.

## Spike 1 — Opus encode load (`spike1_opus_encode/`)

- **Library:** install **arduino-libopus** by Phil Schatzmann
  (`https://github.com/pschatzmann/arduino-libopus`).
- **Question:** can the S3 encode Opus 16 kHz / 24 kbps / 20 ms frames in real time?
- **Pass:** `avg < 10,000 µs/frame` **and** `p95 < 15,000 µs/frame` (real-time
  budget is 20,000 µs; you want headroom because BLE + capture share the chip).
- **If it fails:** pivot to **ADPCM** (4:1, near-free CPU) or raw PCM + Android's
  generous BLE bandwidth. This is the cheapest pivot in the project — find out now.
- The sketch **sweeps `OPUS_SET_COMPLEXITY` 0→10** in one run and prints a table;
  it reports the cheapest complexity that clears the headroom target. Complexity 5+
  runs ~11 ms/frame (real-time but tight); low complexity is usually transparent for
  16 kHz voice and much cheaper. "Pass" = there exists a complexity under threshold.
- **Note:** the encode runs in a dedicated 48 KB FreeRTOS task pinned to core 1, not
  in `loop()`. libopus puts large scratch buffers on the stack, which overflows the
  8 KB `loopTask` stack ("Stack canary watchpoint triggered"). The sketch prints
  remaining stack headroom so you can see how close it ran.

## Spike 2 — microSD (SPI) write throughput (`spike2_sd_throughput/`)

- **No extra library** (uses bundled `SD`/`SPI`).
- Pins (SCK=7, MISO=8, MOSI=9, CS=21) are **confirmed working** on the Sense
  expansion board (card enumerates as 128 GB).
- **Sweeps the SPI clock** (4→40 MHz) and reports MB/s + **worst single-write
  stall** per clock. The 4 MHz default caps at ~0.5 MB/s (= 4 MHz / 8) — that was
  the real bottleneck in the first run, not the card. Higher clocks should clear it.
- **Question:** real sustained MB/s at a usable clock, and the worst-case stall.
- **Pass:** `>= 1.0 MB/s` → VGA MJPEG ~8–10 fps viable. **And** worst stall small
  (>~100 ms ⇒ must PSRAM-buffer + burst-write regardless of average).
- **If it can't clear 1.0 MB/s at any clock:** reduce res/fps or burst-buffer.
- Use a **high-endurance / industrial** card; cheap cards tank sustained writes
  and spike on stalls.

## Run order

Do **Spike 1 then Spike 2** — each is an afternoon and each can independently force
a design pivot. Get both green before investing in the Spike 3 vertical slice
(XIAO → Android → Mac), whose server side is being built in `../server`.

## Phase 0 scorecard — fill this in

| Risk | Assumed | Measured | Verdict | Action |
|---|---|---|---|---|
| Opus on S3 (avg µs/frame) | < 10 ms | **6062 µs @ cplx 1** (5474 @ cplx 0) | ✅ | **keep Opus, lock complexity 1** |
| Opus on S3 (p95 µs/frame) | < 15 ms | **6135 µs @ cplx 1** | ✅ | tail is tight (~70 µs spread) |
| Opus CPU load (%) | < 50% | **30% of core 1 @ cplx 1** | ✅ | core 0 free for BLE/relay |
| SD sustained write (MB/s) | ≥ 1.0 | **1.34 @ 20 MHz** (plateaus above 20) | ✅ | VGA MJPEG viable |
| Implied MJPEG fps (VGA) | ~8–10 | **~34 avg** (target 10–15) | ✅ | 2–3× headroom |
| SD worst-case stall | small | **92 ms** | ⚠️ | **PSRAM burst-buffer required** |
| Whisper RTF (large-v3-turbo, Mac, model alone) | < 0.5 | **0.43 mean / 0.51 p95** | ✅ | continuous STT viable; ~2.3× real time |

**Spike 1 decision (2026-06-30):** Opus 16 kHz / 24 kbps / 20 ms, **complexity 1**.
Sweep showed cplx 0–3 all pass; cplx 5+ fails the headroom target. Bitrate is
rate-controlled so quality is constant across complexity — complexity buys only
CPU, so take the cheap end. ADPCM fallback no longer needed.

**Spike 2 decision (2026-06-30):** SD over SPI **@ 20 MHz** (throughput plateaus
at 1.34 MB/s above 20 MHz — card-bound, not bus-bound; default 4 MHz was the
original bottleneck). Recording Mode = **VGA MJPEG ~10–15 fps, frames staged in a
PSRAM ring buffer and burst-written** to absorb the ~92 ms worst-case GC stall.
Direct-to-SD writing is NOT safe at that stall size.

**Spike 3/4 server decision (2026-06-30):** Whisper `large-v3-turbo` on MLX
measures **RTF 0.43 mean / 0.51 p95** (5 s windows, model alone). Comfortably real
time — keep it as the STT model. Caveat: measured standalone; effective RTF rises
once the extraction LLM, Qwen2.5-VL, and embeddings share the machine — the 0.43
headroom is the budget for that contention. Re-measure under concurrent load before
finalizing the all-warm working set.

Remaining Spike 3/4 work needs the full hardware chain (XIAO → Android → Mac):
- **Spike 3:** multi-hour vertical slice — BLE throughput, battery, VAD, reconnection.
- **Spike 4:** command round-trip + Mac all-warm inference stack (STT + LLM + VL + embeddings concurrent).
