# OpenRecall Hardware Self-Test Firmware

A **throwaway** ESP-IDF firmware you flash onto a bare **XIAO ESP32S3 Sense**
*before* sealing it into the OpenRecall device, to prove every hardware
subsystem is alive and correctly wired. Run it once, read the serial summary
and the LED, and only seal the device when every component is **PASS**.

It exercises the **production** audio and camera drivers (reused by CMake
reference from `../openrecall_sensor/main/`, so it tests the real product code)
and adds the two drivers the product firmware does not yet have: a button on
**D2** (GPIO3) and a battery voltage divider on **D1** (GPIO2 / ADC1_CH1).

## Components tested

| Component | Pad / GPIO | How it's verified |
|-----------|------------|-------------------|
| Two IENMP441 mics | D3-D5 / GPIO4-6 (I2S) | Interactive 3 s capture; PASS if both channels' RMS exceed the noise floor (audible stimulus) **and** the (primary−reference) RMS proves the two channels are distinct. |
| OV2640 camera | Sense board | Two stages: RGB565/QVGA luma std-dev (sensor+optics, lens-cap-off) then the production JPEG/VGA path (SOI/EOI markers + size). |
| Button | D2 / GPIO3 | Interactive; PASS if a press is detected within 10 s. |
| Battery | D1 / GPIO2 (ADC1_CH1) | 100k/100k divider (Vbat = Vadc×2.0); PASS if Vbat ∈ [3.0 V, 4.25 V]. |

## Build & flash (ESP-IDF 5.1.6)

```bash
source ~/esp/esp-idf-v5.1.6/export.sh
cd firmware/openrecall_selftest
idf.py set-target esp32s3      # first time only
idf.py -p <PORT> flash monitor
```

`<PORT>` is the XIAO's USB-C port (e.g. `/dev/cu.usbmodem*`). The console is on
**USB-Serial/JTAG** (the XIAO USB-C port is NOT USB-OTG CDC) — same path the
product firmware uses, so `idf.py monitor` shows the log over the USB-C cable.

## Running it

The rig runs all four tests sequentially on boot and prints a banner, prompts,
and a summary table, then enters a **live mic meter** loop (per-channel RMS every
500 ms) until you press D2 or reset.

```
====== OpenRecall Hardware Self-Test ======
[audio] running...
>>> Please SPEAK or BLOW into BOTH mics for 3 seconds...
[audio] PASS  pri rms=312 ref rms=298 diff=271
[camera] running...
[camera] PASS  luma std=48, jpeg=42118B
[button] running...
>>> Press the D2 button now (waiting up to 10 s)...
[button] PASS  1 press(es) detected
[battery] running...
[battery] PASS  Vbat=4.08V

====== SUMMARY ======
  audio   PASS pri rms=312 ref rms=298 diff=271
  camera  PASS luma std=48, jpeg=42118B
  button  PASS 1 press(es) detected
  battery PASS Vbat=4.08V
=====================
ALL PASS - safe to seal the device.
```

## LED (GPIO21)

The XIAO USER_LED is single-color (and is also the SD CS pin — but this rig
skips SD, so it is free):

- **Solid ON** → all four components PASS. Safe to seal.
- **Blink N times, pause, repeat** → N components FAILED. Do not seal.

The serial log always carries the full detail; the LED is an at-a-glance
indicator. LED polarity (active-low by default) is set in `selftest_config.h`
(`LED_ON`/`LED_OFF`); flip both if your board's LED is active-high.

## Interpreting a FAIL / fault injection

- **audio FAIL** `pri=60 ref=58 diff=2 ...` → no audible stimulus (you didn't
  speak/blow), OR a mic is dead. A high `diff` with low `pri/ref` means the bus
  is alive but no sound; a near-zero `diff` means the two channels are identical
  (collapsed bus / shorted L/R / only one mic working).
- **camera FAIL** `luma std=3.1 (too uniform: lens cap / dead sensor)` → lens
  cap on, sensor dark, or a dead sensor. `jpeg bad: ...` → the production JPEG
  path failed (wiring/SCCB/DVP). Re-init timing is the usual culprit; reflash and
  retry once.
- **button FAIL** `no press within 10s` → wiring (button not to GND), wrong
  pad, or stuck-high. Note D2/GPIO3 is a **strapping pin**: a button→GND with the
  internal pull-up is safe as long as it is **not held during reset**.
- **battery FAIL** `Vbat=0.05V (divider not connected?)` → divider not wired or
  wrong pad. `Vbat=2.9V out of range` → low/empty cell or wrong divider ratio
  (check the `VBAT_DIVIDER` constant in `selftest_config.h`).

## Configuration

All pins, the divider ratio, and test thresholds live in
`main/selftest_config.h`. The reused driver pin maps live in the production
`../openrecall_sensor/main/config.h` (shared via INCLUDE_DIRS).

## Out of scope

SD card, BLE, WiFi, Opus, and provisioning are intentionally **not** tested by
this rig. It contains no radio and no SD code, so it can't tell you anything
about those subsystems.