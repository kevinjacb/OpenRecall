/*
 * D1 button firmware wiring (ESP-only) — GPIO2 level sampling + debounce +
 * the gesture classifier (button.c) + gesture -> command dispatch (spec 3.4).
 *
 * The pure-C classifier lives in button.c (host-testable, no ESP deps). This
 * file is the ESP-only glue: it polls the debounced GPIO level every
 * BUTTON_DEBOUNCE_MS, feeds the classifier, and dispatches the resolved
 * gesture through the executor queue (executor_dispatch_local). Keeping the
 * GPIO/task code out of button.c is what lets button.c link + test on the host
 * without the ESP toolchain.
 *
 * Gesture -> action (spec 3.4):
 *   SHORT     -> request_buffer 60 s (mark moment)
 *   DOUBLE    -> toggle capture (start_audio if stopped, stop_audio if capturing)
 *   LONG      -> capture_photo (one-shot snapshot now)
 *   VERY_LONG -> deep sleep (explicit only; wakes on the next press)
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Configure GPIO2 (input + pull-up) and start the button gesture task.
 * Call once from app_main after executor_init. Non-fatal on failure. */
esp_err_t button_start(void);

#ifdef __cplusplus
}
#endif