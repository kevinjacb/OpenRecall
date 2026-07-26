/*
 * Host-side test for the software VAD state machine (no ESP toolchain).
 *
 *   cc -std=c11 -I../main ../main/vad.c test_vad.c -o /tmp/vadtest && /tmp/vadtest
 */
#include "config.h"
#include "vad.h"

#include <stdio.h>
#include <string.h>

#define N FRAME_SAMPLES

static int failures = 0;

static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

// Fill a frame with a constant amplitude (mean-square == amp*amp).
static void fill(int16_t *buf, int16_t amp) {
  for (int i = 0; i < N; i++) buf[i] = amp;
}

int main(void) {
  printf("software VAD host test\n");

  const uint32_t threshold = 1000000;  // mean-square gate (amp 1000 -> 1e6)
  const int hangover = 3;              // short hangover for the test

  int16_t loud[N], quiet[N];
  fill(loud, 2000);   // mean-square 4e6 > threshold -> voiced
  fill(quiet, 100);   // mean-square 1e4 < threshold -> unvoiced

  vad_t v;
  vad_init(&v, threshold, hangover);

  check("loud frame is SPEECH", vad_process(&v, loud, N) == C6_SPEECH);

  // After speech ends, exactly `hangover` HANGOVER frames, then GAP.
  check("hangover 1", vad_process(&v, quiet, N) == C6_HANGOVER);
  check("hangover 2", vad_process(&v, quiet, N) == C6_HANGOVER);
  check("hangover 3", vad_process(&v, quiet, N) == C6_HANGOVER);
  check("then GAP_MARKER", vad_process(&v, quiet, N) == C6_GAP_MARKER);
  check("sustained silence stays GAP", vad_process(&v, quiet, N) == C6_GAP_MARKER);

  // Voiced again re-arms hangover.
  check("re-onset is SPEECH", vad_process(&v, loud, N) == C6_SPEECH);
  check("hangover re-armed", vad_process(&v, quiet, N) == C6_HANGOVER);

  // Cold start in silence is a gap, not spurious speech.
  vad_t v2;
  vad_init(&v2, threshold, hangover);
  check("cold start silence is GAP", vad_process(&v2, quiet, N) == C6_GAP_MARKER);

  /* ---- Dual-channel ratio VAD ---- */
  printf("dual-channel VAD host test\n");
  {
    const uint32_t threshold = 1000000;  /* mean-square gate (amp 1000 -> 1e6) */
    const int hangover = 3;
    int16_t loud_p[N], loud_r[N], quiet2[N], voice_p[N], amb_r[N];
    fill(loud_p, 2000);  fill(loud_r, 2000);   /* loud correlated ambient on BOTH */
    fill(quiet2, 100);
    fill(voice_p, 2000);                       /* voice on primary, above threshold */
    fill(amb_r, 500);                          /* mild ambient on reference */

    vad_t vd;
    vad_init(&vd, threshold, hangover);

    /* Loud correlated ambient: passes energy gate, ratio ~1 -> rejected. */
    check("loud correlated ambient -> GAP (junk rejected)",
          vad_process_dual(&vd, loud_p, loud_r, N) == C6_GAP_MARKER);

    /* Voice above threshold, mild ambient on reference: ratio high -> SPEECH. */
    check("voice+ambient -> SPEECH",
          vad_process_dual(&vd, voice_p, amb_r, N) == C6_SPEECH);

    /* Reference dead (e_ref ~ 0): ratio -> inf -> pure energy VAD fallback. */
    check("reference dead, loud primary -> SPEECH",
          vad_process_dual(&vd, loud_p, quiet2, N) == C6_SPEECH);

    /* Hangover after speech ends. */
    check("hangover 1", vad_process_dual(&vd, quiet2, quiet2, N) == C6_HANGOVER);
    check("hangover 2", vad_process_dual(&vd, quiet2, quiet2, N) == C6_HANGOVER);
    check("hangover 3", vad_process_dual(&vd, quiet2, quiet2, N) == C6_HANGOVER);
    check("then GAP", vad_process_dual(&vd, quiet2, quiet2, N) == C6_GAP_MARKER);

    /* Cold start in correlated ambient is a gap, not spurious speech. */
    vad_t vd2;
    vad_init(&vd2, threshold, hangover);
    check("cold start ambient -> GAP",
          vad_process_dual(&vd2, loud_p, loud_r, N) == C6_GAP_MARKER);
  }

  printf(failures ? "\nFAILED (%d)\n" : "\nOK\n", failures);
  return failures ? 1 : 0;
}
