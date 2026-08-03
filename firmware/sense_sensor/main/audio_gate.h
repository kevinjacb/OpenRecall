/*
 * Audio capture pause/resume gate (§D start_audio / stop_audio).
 *
 * One writer (executor task, core 0) + one reader (audio_task, core 1). Backed by
 * a single volatile bool — no mutex. A torn read at worst costs one extra/missed
 * 20 ms frame, which the gap-marker path already tolerates. Reads/writes of an
 * aligned bool are single-instruction on Xtensa LX7. Portable (no ESP headers).
 *
 * Default (static zero-init) = NOT paused: audio is always-on at boot, byte-
 * identical to today until a stop_audio command arrives.
 */
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

void audio_gate_set(bool paused);   /* writer: executor task, core 0 */
bool audio_gate_paused(void);         /* reader: audio_task, core 1   */

#ifdef __cplusplus
}
#endif