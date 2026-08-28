/*
 * Button gesture classifier implementation (pure C, host-testable).
 * See button.h for the poll-driven contract and gesture semantics.
 */
#include "button.h"
#include "config.h"

void button_classifier_init(button_classifier_t *b, uint32_t now_ms) {
  (void)now_ms;
  b->state = BTN_STATE_IDLE;
  b->anchor_ms = 0;
  b->second_tap = false;
}

button_gesture_t button_classifier_event(button_classifier_t *b, bool pressed, uint32_t now_ms) {
  button_gesture_t g = BTN_NONE;
  uint32_t dt = now_ms - b->anchor_ms;  /* time since the current anchor */

  switch (b->state) {
    case BTN_STATE_IDLE:
      if (pressed) {
        b->anchor_ms = now_ms;
        b->second_tap = false;
        b->state = BTN_STATE_PRESSED;
      }
      break;

    case BTN_STATE_PRESSED:
      if (pressed) {
        /* still held — very-long fires in-band so the user gets feedback. */
        if (dt >= BUTTON_VERY_LONG_MS) {
          g = BTN_VERY_LONG;
          b->state = BTN_STATE_IDLE;
          b->second_tap = false;
        }
      } else {
        /* release */
        if (dt >= BUTTON_VERY_LONG_MS) {
          /* crossed very-long without an in-band poll tick — belt-and-suspenders */
          g = BTN_VERY_LONG;
          b->state = BTN_STATE_IDLE;
          b->second_tap = false;
        } else if (dt >= BUTTON_LONG_MS) {
          g = BTN_LONG;
          b->state = BTN_STATE_IDLE;
          b->second_tap = false;
        } else {
          /* short tap. If this is the second tap of a double, emit DOUBLE;
           * otherwise wait for a possible second tap within the gap. */
          if (b->second_tap) {
            g = BTN_DOUBLE;
            b->state = BTN_STATE_IDLE;
            b->second_tap = false;
          } else {
            b->anchor_ms = now_ms;  /* anchor = release time for the double gap */
            b->state = BTN_STATE_WAIT_DOUBLE;
          }
        }
      }
      break;

    case BTN_STATE_WAIT_DOUBLE:
      if (pressed) {
        if (dt <= BUTTON_DOUBLE_GAP_MS) {
          /* second tap within the gap -> measure its hold; short release = DOUBLE */
          b->anchor_ms = now_ms;
          b->second_tap = true;
          b->state = BTN_STATE_PRESSED;
        } else {
          /* gap already expired -> the first was a single SHORT; emit it and
           * treat this press as a fresh candidate. */
          g = BTN_SHORT;
          b->anchor_ms = now_ms;
          b->second_tap = false;
          b->state = BTN_STATE_PRESSED;
        }
      } else if (dt > BUTTON_DOUBLE_GAP_MS) {
        /* still released, no second tap, gap expired -> single SHORT. */
        g = BTN_SHORT;
        b->state = BTN_STATE_IDLE;
        b->second_tap = false;
      }
      break;
  }
  return g;
}