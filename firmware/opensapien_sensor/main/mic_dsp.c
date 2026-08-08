#include "mic_dsp.h"

static inline int16_t sat_int16(int32_t v) {
  if (v > 32767)  return 32767;
  if (v < -32768) return -32768;
  return (int16_t)v;
}

void mic_dsp_init(mic_dsp_t *d) {
  for (int k = 0; k < DSP_NLMS_TAPS; k++) {
    d->w[k] = 0;
    d->delay[k] = 0;
  }
  d->didx = 0;
}

void mic_dsp_process(mic_dsp_t *d, const int16_t *primary, const int16_t *reference,
                     int16_t *out, size_t n, bool adapt_now) {
  const int L = DSP_NLMS_TAPS;
  for (size_t i = 0; i < n; i++) {
    /* Push newest reference sample into the delay ring at didx. */
    int16_t x = reference[i];
    d->delay[d->didx] = x;

    /* Filtered reference: y_q15 = sum_k w[k] * delay[(didx - k + L) % L].
     * w is Q15, delay is Q0 (int16) -> product is Q15; accumulate in int64. */
    int64_t acc = 0;
    int64_t pow = 0;  /* ||delay||^2, Q0 (sum of int16^2) */
    for (int k = 0; k < L; k++) {
      int idx = (d->didx - k + L) % L;   /* lag-k sample */
      int32_t dk = d->delay[idx];
      acc += (int64_t)d->w[k] * (int64_t)dk;
      pow += (int64_t)dk * (int64_t)dk;
    }
    int32_t y_q15 = (int32_t)(acc >> 15);
    int32_t y = (int32_t)primary[i] - y_q15;   /* cleaned output (Q0) */
    out[i] = sat_int16(y);

    if (adapt_now) {
      /* Widrow: the adaptation error is the output itself (minimize output
       * power on noise-only frames -> drive filter toward the noise path). */
      int32_t e = y;
      int64_t denom = pow + (int64_t)DSP_NLMS_EPS;
      if (denom > 0) {
        for (int k = 0; k < L; k++) {
          int idx = (d->didx - k + L) % L;
          /* NLMS in Q15: w[k] += mu_q15 * e * x[k] / (||x||^2 + eps) */
          int64_t upd = ((int64_t)DSP_NLMS_STEP_Q15 * (int64_t)e * (int64_t)d->delay[idx]) / denom;
          d->w[k] = (int32_t)(d->w[k] + upd);
          /* leakage: gently pull w toward 0 to bound drift */
          d->w[k] -= (d->w[k] * (int32_t)DSP_NLMS_LEAK_Q15) >> 15;
        }
      }
    }

    d->didx = (d->didx + 1) % L;
  }
}