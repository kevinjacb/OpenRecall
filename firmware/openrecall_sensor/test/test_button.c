/*
 * Host-side test for the button gesture classifier (no ESP toolchain).
 *   cc -std=c11 -I../main ../main/button.c test_button.c -o /tmp/btntest && /tmp/btntest
 *
 * Poll-driven: feed (current_level, monotonic_ms) at a 20 ms tick. The
 * classifier resolves short / double / long / very-long from the level
 * pattern. See button.h.
 */
#include "button.h"

#include <stdio.h>

#define TICK 20

static int failures = 0;
static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

/* Feed one tick; shorthand for the common "expect no gesture" case. */
static button_gesture_t feed(button_classifier_t *b, bool pressed, uint32_t now_ms) {
  return button_classifier_event(b, pressed, now_ms);
}

int main(void) {
  printf("button gesture classifier:\n");
  button_classifier_t b;

  /* ---- SHORT: tap 0..200, then gap expires at >600 ---- */
  button_classifier_init(&b, 0);
  check("short: press ticks -> NONE", feed(&b, true, 0) == BTN_NONE);
  for (uint32_t t = TICK; t <= 200; t += TICK)
    check("short: held -> NONE", feed(&b, true, t) == BTN_NONE);
  check("short: release -> NONE (waits for gap)", feed(&b, false, 200) == BTN_NONE);
  check("short: gap 400 (t=600) still waiting", feed(&b, false, 600) == BTN_NONE);
  check("short: gap >400 (t=620) -> SHORT", feed(&b, false, 620) == BTN_SHORT);
  check("short: after emit, idle NONE", feed(&b, false, 640) == BTN_NONE);

  /* ---- DOUBLE: two taps, second press within the 400 ms gap ---- */
  button_classifier_init(&b, 0);
  for (uint32_t t = 0; t <= 200; t += TICK) feed(&b, true, t);   /* first tap */
  check("double: first release -> NONE", feed(&b, false, 200) == BTN_NONE);
  check("double: second press (t=500, gap=300) -> NONE", feed(&b, true, 500) == BTN_NONE);
  for (uint32_t t = 520; t <= 700; t += TICK) feed(&b, true, t); /* second tap held */
  check("double: second release -> DOUBLE", feed(&b, false, 700) == BTN_DOUBLE);

  /* ---- LONG: hold 0..1480, release at 1500 (>=1000, <6000) ---- */
  button_classifier_init(&b, 0);
  for (uint32_t t = 0; t <= 1480; t += TICK)
    check("long: held -> NONE", feed(&b, true, t) == BTN_NONE);
  check("long: release at 1500 -> LONG", feed(&b, false, 1500) == BTN_LONG);

  /* ---- LONG boundary: release exactly at 1000 -> LONG ---- */
  button_classifier_init(&b, 0);
  for (uint32_t t = 0; t <= 980; t += TICK) feed(&b, true, t);
  check("long-boundary: release at 1000 -> LONG", feed(&b, false, 1000) == BTN_LONG);

  /* ---- VERY_LONG: held crosses 6000 in-band ---- */
  button_classifier_init(&b, 0);
  for (uint32_t t = 0; t <= 5980; t += TICK)
    check("very-long: held -> NONE", feed(&b, true, t) == BTN_NONE);
  check("very-long: held 6000 -> VERY_LONG (in-band)", feed(&b, true, 6000) == BTN_VERY_LONG);
  check("very-long: after emit, idle NONE", feed(&b, true, 6020) == BTN_NONE);

  /* ---- short then long (NOT a double): short emits on gap, then a fresh long ---- */
  button_classifier_init(&b, 0);
  for (uint32_t t = 0; t <= 200; t += TICK) feed(&b, true, t);   /* first tap */
  feed(&b, false, 200);                                          /* -> WAIT_DOUBLE */
  check("short+long: gap expired (t=620) -> SHORT", feed(&b, false, 620) == BTN_SHORT);
  check("short+long: fresh press (t=1000) -> NONE", feed(&b, true, 1000) == BTN_NONE);
  for (uint32_t t = 1020; t <= 2180; t += TICK) feed(&b, true, t);
  check("short+long: release at 2200 -> LONG", feed(&b, false, 2200) == BTN_LONG);

  /* ---- second tap held LONG is a LONG, not a DOUBLE ---- */
  button_classifier_init(&b, 0);
  for (uint32_t t = 0; t <= 200; t += TICK) feed(&b, true, t);   /* first short tap */
  feed(&b, false, 200);                                          /* -> WAIT_DOUBLE */
  feed(&b, true, 500);                                           /* second press within gap */
  for (uint32_t t = 520; t <= 1480; t += TICK)
    check("dbl+long: second held -> NONE", feed(&b, true, t) == BTN_NONE);
  check("dbl+long: second release at 1500 -> LONG (not DOUBLE)",
        feed(&b, false, 1500) == BTN_LONG);

  /* ---- debounce: held-through extra pressed ticks, then a clean short ----
   * Press at 0/50/100 are all PRESSED (held, no very-long). Release at 150 ->
   * WAIT_DOUBLE (anchor=150). Gap expires when dt > 400, i.e. t > 550; at
   * t=600 dt=450 -> SHORT. Held-through noise did not split the tap. */
  button_classifier_init(&b, 0);
  feed(&b, true, 0); feed(&b, true, 50); feed(&b, true, 100);    /* bounce/hold */
  check("bounce: held-through -> NONE on release", feed(&b, false, 150) == BTN_NONE);
  check("bounce: gap expired (t=600, dt=450) -> SHORT", feed(&b, false, 600) == BTN_SHORT);
  check("bounce: after emit -> NONE", feed(&b, false, 620) == BTN_NONE);

  /* ---- idle noise: released ticks while IDLE never emit ---- */
  button_classifier_init(&b, 0);
  check("idle: released ticks -> NONE", feed(&b, false, 0) == BTN_NONE);
  check("idle: more released -> NONE", feed(&b, false, 1000) == BTN_NONE);

  if (failures) {
    printf("FAIL (%d)\n", failures);
    return 1;
  }
  printf("PASS\n");
  return 0;
}