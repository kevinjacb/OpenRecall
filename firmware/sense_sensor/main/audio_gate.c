#include "audio_gate.h"

static volatile bool s_paused = false;

void audio_gate_set(bool paused) { s_paused = paused; }
bool audio_gate_paused(void)      { return s_paused; }