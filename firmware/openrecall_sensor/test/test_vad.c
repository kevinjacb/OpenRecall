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

  /* ---- Single-channel energy VAD (runtime audio path) ---- */
  printf("single-channel VAD host test\n");
  {
    const uint32_t threshold = 200000;  /* mean-square gate (amp 447 -> 2e5) */
    const int hangover = 3;
    int16_t silence[N], speech[N], quiet[N];
    fill(silence, 100);   /* room ambient: amp 100 -> ms 1e4, below gate */
    fill(speech, 800);    /* wearable-distance speech: amp 800 -> ms 6.4e5, above gate */
    fill(quiet, 200);     /* near-threshold noise: amp 200 -> ms 4e4, below gate */

    vad_t vd;
    vad_init(&vd, threshold, hangover);

    /* Cold start: silence below the gate -> GAP (no spurious speech). */
    check("silence -> GAP", vad_process_single(&vd, silence, N) == C6_GAP_MARKER);
    /* Sub-threshold noise (amp 200 -> ms 4e4) below the 2e5 gate -> GAP. */
    check("sub-threshold noise -> GAP", vad_process_single(&vd, quiet, N) == C6_GAP_MARKER);
    /* Speech above the gate -> SPEECH (arms hangover). */
    check("speech -> SPEECH", vad_process_single(&vd, speech, N) == C6_SPEECH);
    /* Hangover after speech ends (3 frames, then GAP). */
    check("hangover 1", vad_process_single(&vd, silence, N) == C6_HANGOVER);
    check("hangover 2", vad_process_single(&vd, silence, N) == C6_HANGOVER);
    check("hangover 3", vad_process_single(&vd, silence, N) == C6_HANGOVER);
    check("then GAP", vad_process_single(&vd, silence, N) == C6_GAP_MARKER);
    /* Cold start in sub-threshold noise is a gap. */
    vad_t vd2;
    vad_init(&vd2, threshold, hangover);
    check("cold start noise -> GAP", vad_process_single(&vd2, quiet, N) == C6_GAP_MARKER);
  }

  /* ---- Adaptive noise-floor VAD (runtime audio path) ---- */
  printf("adaptive noise-floor VAD host test\n");
  {
    const uint32_t floor = 10000;   /* VAD_ENERGY_THRESHOLD_FLOOR */
    const uint32_t mult = 3;        /* VAD_NOISE_MULTIPLIER */
    const int hangover = 3;
    int16_t quiet[N], speech[N], mid[N], noisy80[N];
    fill(quiet, 70);    /* room noise: ms 4900, below floor */
    fill(speech, 300);  /* wearable-distance speech: ms 9e4 */
    fill(mid, 200);     /* quiet consonant: ms 4e4 — below fixed 5e4, above adaptive */
    fill(noisy80, 80);  /* gradual noise: ms 6400, sub-threshold, < 1.5*5e3 */

    /* Cold start in a quiet room: floor seeds to 1e4, thresh = max(1e4,3e4)=3e4.
     * Room noise 4900 < 3e4 -> GAP. */
    vad_t va;
    vad_init_adaptive(&va, floor, mult, hangover);
    check("cold start quiet -> GAP",
          vad_process_single_adaptive(&va, quiet, N) == C6_GAP_MARKER);

    /* 50 quiet frames (1 s) decay the floor from 1e4 toward 4900; thresh ->
     * max(1e4, ~5e3*3) = 1.5e4. A quiet consonant at 4e4 > 1.5e4 -> SPEECH.
     * The fixed 5e4 would call 4e4 a GAP -> never encoded. Core win. */
    for (int i = 0; i < 50; i++) vad_process_single_adaptive(&va, quiet, N);
    check("sub-fixed-threshold consonant -> SPEECH (adaptive win)",
          vad_process_single_adaptive(&va, mid, N) == C6_SPEECH);
    /* speech frame must not raise the floor (frozen on speech). */
    check("floor unchanged by speech frame", va.noise_floor < 7000);

    /* Floor falls fast: seed 2e4 (noisy room), go quiet, drops in ~1 s. */
    vad_t vb;
    vad_init_adaptive(&vb, 20000, mult, hangover);
    for (int i = 0; i < 50; i++) vad_process_single_adaptive(&vb, quiet, N);
    check("floor falls fast in quiet (under 12000)", vb.noise_floor < 12000);

    /* Floor rises slowly on gradual noise. Seed 5e3, feed ms-6400 frames
     * (sub-threshold 1.5e4, and 6400 < 5e3*1.5=7500 so rise is allowed).
     * 100 frames at alpha_up 0.002: floor ~5e3*0.82+6400*0.18 ~ 5260. */
    vad_t vc;
    vad_init_adaptive(&vc, 5000, mult, hangover);
    for (int i = 0; i < 100; i++) vad_process_single_adaptive(&vc, noisy80, N);
    check("floor rises slowly on gradual noise (over 5100)", vc.noise_floor > 5100);
    check("floor rises only slowly (under 6000)", vc.noise_floor < 6000);

    /* Loud burst guard: seed 5e3, feed a burst at ms 1.44e4 (amp 120). It is
     * sub-threshold (1.44e4 < 1.5e4) so GAP, but 1.44e4 > 5e3*1.5=7500 so the
     * rise guard REJECTS it — the floor must NOT jump toward the burst. */
    vad_t vd;
    vad_init_adaptive(&vd, 5000, mult, hangover);
    int16_t burst120[N]; fill(burst120, 120);
    for (int i = 0; i < 20; i++) vad_process_single_adaptive(&vd, burst120, N);
    check("loud burst does not raise floor (guard)", vd.noise_floor < 5200);

    /* Hangover still works on the adaptive path. */
    vad_t ve;
    vad_init_adaptive(&ve, floor, mult, hangover);
    vad_process_single_adaptive(&ve, speech, N);   /* SPEECH, arms hangover */
    check("adaptive hangover 1",
          vad_process_single_adaptive(&ve, quiet, N) == C6_HANGOVER);
    check("adaptive hangover 2",
          vad_process_single_adaptive(&ve, quiet, N) == C6_HANGOVER);
    check("adaptive hangover 3",
          vad_process_single_adaptive(&ve, quiet, N) == C6_HANGOVER);
    check("adaptive then GAP",
          vad_process_single_adaptive(&ve, quiet, N) == C6_GAP_MARKER);
  }

  printf(failures ? "\nFAILED (%d)\n" : "\nOK\n", failures);
  return failures ? 1 : 0;
}
