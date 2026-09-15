/*
 * OpenRecall self-test firmware — test result types + declarations.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum { ST_PASS = 0, ST_FAIL, ST_SKIP } selftest_status_t;

typedef struct {
  selftest_status_t status;
  char detail[96];
} selftest_result_t;

/* LED reporter (GPIO21). */
void selftest_led_init(void);
void selftest_led_set_all_pass(void);          /* solid ON */
void selftest_led_start_fail_blink(int n);     /* blink N times, pause, repeat */

/* Component tests (each returns a result + measured detail string). */
selftest_result_t test_battery(void);
selftest_result_t test_button(void);
selftest_result_t test_audio(void);
selftest_result_t test_camera(void);