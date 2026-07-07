/*
 * libopus build configuration for the Sense AI Sensor (ESP32-S3, Xtensa LX7).
 *
 * Hand-written minimum config — replaces the autotools/meson-generated
 * config.h. We compile a fixed-point build (no float API, no DNN/Deep-PLC/
 * DRED/OSCE/LossGen features) on a plain Xtensa core (no NEON, no SSE, no
 * MIPS), so the upstream CPU-detection branches are all inactive.
 *
 * Why hand-written: the upstream build assumes a Unix host with autoconf and
 * a general-purpose x86/arm compiler; ESP-IDF's CMake can't run those checks,
 * and the upstream config.h is the output of those checks, not a source file.
 * This file pins the build to the Spike-1-measured parameters
 * (firmware/README.md: 16 kHz / 24 kbps / 20 ms / complexity 1, ~6 ms/frame).
 *
 * Pair with components/opus/CMakeLists.txt which defines the matching compile
 * flags (HAVE_CONFIG_H, OPUS_BUILD, FIXED_POINT) and excludes the float and
 * DNN code paths at the source-list level.
 */
#ifndef OPUS_CONFIG_H
#define OPUS_CONFIG_H

/* ---- Build profile (matches Spike 1 measurement) ---- */
/* Fixed-point build: cheaper on a non-FPU Xtensa LX7 than the float API. */
#define FIXED_POINT 1
/* We don't need the float encoder/decoder/analysis API. Saves ~30 KB of code. */
#define DISABLE_FLOAT_API 1
/* We don't need non-standard sample rates / non-20-ms frames. Saves a few KB. */
#undef CUSTOM_MODES
/* DRED (Deep PLC) and Deep PLC are float-heavy and reference the dnn/ tree
 * that we don't include. Leave both off so the encoder skips them entirely. */
#undef ENABLE_DRED
#undef ENABLE_DEEP_PLC
#undef ENABLE_OSCE
#undef ENABLE_LOSSGEN
/* Standard-mode bitstream only. */
#undef DISABLE_UPDATE_DRAFT

/* ---- Feature toggles ---- */
#define ENABLE_ASSERTIONS 0       /* release build; the assertions are debug-only */
#undef ENABLE_HARDENING           /* would require mbedTLS hardening hooks */
#undef FIXED_DEBUG
#undef FUZZING

/* ---- Compiler/platform capability flags (autoconf would have set these) ---- */
#define HAVE_STDINT_H 1
#define HAVE_INTTYPES_H 1
#define HAVE_STDIO_H 1
/* lrintf/lrint: ESP32-S3's newlib has them. Defining them as 1 means the
 * fixed-point layer can use them; defining 0 falls back to inline rounding
 * (slower). */
#define HAVE_LRINT 1
#define HAVE_LRINTF 1
/* C99 VLA support: xtensa-esp32-elf-gcc supports VLAs, so VAR_ARRAYS is
 * faster than alloca. */
#define VAR_ARRAYS 1
#undef USE_ALLOCA
#undef HAVE_ALLOCA_H

/* ---- CPU architecture: Xtensa, no SIMD ---- */
/* We don't define any OPUS_ARM_*, OPUS_X86_*, OPUS_MIPS_*, or BFIN_ASM macros
 * because none of those instruction sets apply. With all of them undefined,
 * celt/arch.h falls through to the generic C paths in arm/fixed_generic.h,
 * x86/fixed_generic.h, etc. — pure C, no vector intrinsics. The Xtensa LX7
 * doesn't have a fast path here, so the encoder is the plain C reference. */

/* ---- DNN (neural-net denoiser): excluded entirely ---- */
/* The dnn/ source tree is not vendored; never reference it. */
#undef HAVE_ARM_NE10

#endif /* OPUS_CONFIG_H */
