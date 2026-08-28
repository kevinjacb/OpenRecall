/*
 * Button gesture classifier — pure-C poll-driven state machine for the D1
 * push button (spec 3.4).
 *
 * Call button_classifier_event() at a regular tick (e.g. 20 ms) with the
 * CURRENT debounced button level (pressed=true while held LOW, false while
 * released HIGH) and a monotonic ms timestamp. A gesture (short / double /
 * long / very-long) is returned when the hold/release pattern resolves, else
 * BTN_NONE. Pure C / no ESP deps / host-testable — the GPIO level sampling +
 * debounce live in the firmware wiring (button_task), not here.
 *
 * Why poll-driven (not edge-driven): a single short tap must emit SHORT once
 * the double-gap window expires, even if the user never presses again. An
 * edge-only machine has no event to fire that timeout; polling gives the
 * classifier a clock to expire the window on its own.
 *
 * Gestures (spec 3.4):
 *   SHORT     -> mark moment (request_buffer 60 s)
 *   DOUBLE    -> toggle capture (audio_gate)
 *   LONG      -> capture_photo (snapshot now)
 *   VERY_LONG -> deep sleep (explicit only)
 *
 * Semantics:
 *   - SHORT: a tap held < BUTTON_LONG_MS, not followed by a second tap within
 *     BUTTON_DOUBLE_GAP_MS of its release. Emits when the gap expires.
 *   - DOUBLE: two taps each held < BUTTON_LONG_MS, the second press arriving
 *     within BUTTON_DOUBLE_GAP_MS of the first release. Emits on the second
 *     release. (A second tap held >= LONG_MS is a LONG, not a double — a
 *     deliberate long hold signals snapshot intent.)
 *   - LONG: a single hold of BUTTON_LONG_MS..BUTTON_VERY_LONG_MS. Emits on
 *     release.
 *   - VERY_LONG: a hold >= BUTTON_VERY_LONG_MS. Emits IN-BAND (while still
 *     held) so the user gets feedback (LED) without waiting for release.
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  BTN_NONE = 0,
  BTN_SHORT,
  BTN_DOUBLE,
  BTN_LONG,
  BTN_VERY_LONG,
} button_gesture_t;

typedef enum {
  BTN_STATE_IDLE = 0,
  BTN_STATE_PRESSED,
  BTN_STATE_WAIT_DOUBLE,
} button_state_t;

typedef struct {
  button_state_t state;
  uint32_t anchor_ms;  /* press time (PRESSED) or short-release time (WAIT_DOUBLE) */
  bool second_tap;     /* true while PRESSED is the second tap of a potential double */
} button_classifier_t;

void button_classifier_init(button_classifier_t *b, uint32_t now_ms);

/* Feed one tick: pressed = current debounced level, now_ms = monotonic ms.
 * Returns a gesture when one resolves, else BTN_NONE. */
button_gesture_t button_classifier_event(button_classifier_t *b, bool pressed, uint32_t now_ms);

#ifdef __cplusplus
}
#endif