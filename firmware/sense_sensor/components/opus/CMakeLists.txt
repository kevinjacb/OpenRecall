# libopus 1.5.2 — vendored as an ESP-IDF component for the Sense AI Sensor.
#
# Vendored from the official xiph.org opus 1.5.2 tarball
# (https://github.com/xiph/opus/releases/tag/v1.5.2) and trimmed for the
# XIAO ESP32S3 Sense target:
#   - Generic C sources only (no x86/arm/mips SIMD; Xtensa LX7 has no NEON)
#   - Fixed-point build (no float API, no DNN/Deep-PLC/DRED/OSCE/LossGen)
#   - No tests, no autotools/meson artifacts, no doc
#
# The remaining source tree lives in opus-1.5.2/ subdirectory. The upstream
# CMakeLists is intentionally NOT used (it targets Unix with autoconf-driven
# feature detection); this file is the build recipe for ESP-IDF.
#
# The header layout mirrors the upstream convention so that opus.c, celt/,
# silk/, and dnn/ sources can all use the same #include paths:
#   - "opus.h"          (the public API; consumer includes this)
#   - "celt/..."        (internal CELT headers)
#   - "silk/..."        (internal SILK headers)
#   - "dnn/..."         (internal DNN headers, not vendored)
#   - "config.h"        (this component's build configuration)
# Consumers get the include directories on the public interface.

idf_component_register(
    SRCS
        "opus-1.5.2/celt/bands.c"
        "opus-1.5.2/celt/celt_decoder.c"
        "opus-1.5.2/celt/celt_encoder.c"
        "opus-1.5.2/celt/celt_lpc.c"
        "opus-1.5.2/celt/celt.c"
        "opus-1.5.2/celt/cwrs.c"
        "opus-1.5.2/celt/entcode.c"
        "opus-1.5.2/celt/entdec.c"
        "opus-1.5.2/celt/entenc.c"
        "opus-1.5.2/celt/kiss_fft.c"
        "opus-1.5.2/celt/laplace.c"
        "opus-1.5.2/celt/mathops.c"
        "opus-1.5.2/celt/mdct.c"
        "opus-1.5.2/celt/modes.c"
        "opus-1.5.2/celt/pitch.c"
        "opus-1.5.2/celt/quant_bands.c"
        "opus-1.5.2/celt/rate.c"
        "opus-1.5.2/celt/vq.c"
        "opus-1.5.2/silk/A2NLSF.c"
        "opus-1.5.2/silk/ana_filt_bank_1.c"
        "opus-1.5.2/silk/biquad_alt.c"
        "opus-1.5.2/silk/bwexpander_32.c"
        "opus-1.5.2/silk/bwexpander.c"
        "opus-1.5.2/silk/check_control_input.c"
        "opus-1.5.2/silk/CNG.c"
        "opus-1.5.2/silk/code_signs.c"
        "opus-1.5.2/silk/control_audio_bandwidth.c"
        "opus-1.5.2/silk/control_codec.c"
        "opus-1.5.2/silk/control_SNR.c"
        "opus-1.5.2/silk/debug.c"
        "opus-1.5.2/silk/dec_API.c"
        "opus-1.5.2/silk/decode_core.c"
        "opus-1.5.2/silk/decode_frame.c"
        "opus-1.5.2/silk/decode_indices.c"
        "opus-1.5.2/silk/decode_parameters.c"
        "opus-1.5.2/silk/decode_pitch.c"
        "opus-1.5.2/silk/decode_pulses.c"
        "opus-1.5.2/silk/decoder_set_fs.c"
        "opus-1.5.2/silk/enc_API.c"
        "opus-1.5.2/silk/encode_indices.c"
        "opus-1.5.2/silk/encode_pulses.c"
        "opus-1.5.2/silk/fixed/apply_sine_window_FIX.c"
        "opus-1.5.2/silk/fixed/autocorr_FIX.c"
        "opus-1.5.2/silk/fixed/burg_modified_FIX.c"
        "opus-1.5.2/silk/fixed/corrMatrix_FIX.c"
        "opus-1.5.2/silk/fixed/encode_frame_FIX.c"
        "opus-1.5.2/silk/fixed/find_LPC_FIX.c"
        "opus-1.5.2/silk/fixed/find_LTP_FIX.c"
        "opus-1.5.2/silk/fixed/find_pitch_lags_FIX.c"
        "opus-1.5.2/silk/fixed/find_pred_coefs_FIX.c"
        "opus-1.5.2/silk/fixed/k2a_FIX.c"
        "opus-1.5.2/silk/fixed/k2a_Q16_FIX.c"
        "opus-1.5.2/silk/fixed/LTP_analysis_filter_FIX.c"
        "opus-1.5.2/silk/fixed/LTP_scale_ctrl_FIX.c"
        "opus-1.5.2/silk/fixed/noise_shape_analysis_FIX.c"
        "opus-1.5.2/silk/fixed/pitch_analysis_core_FIX.c"
        "opus-1.5.2/silk/fixed/process_gains_FIX.c"
        "opus-1.5.2/silk/fixed/regularize_correlations_FIX.c"
        "opus-1.5.2/silk/fixed/residual_energy_FIX.c"
        "opus-1.5.2/silk/fixed/residual_energy16_FIX.c"
        "opus-1.5.2/silk/fixed/schur_FIX.c"
        "opus-1.5.2/silk/fixed/schur64_FIX.c"
        "opus-1.5.2/silk/fixed/vector_ops_FIX.c"
        "opus-1.5.2/silk/fixed/warped_autocorrelation_FIX.c"
        "opus-1.5.2/silk/gain_quant.c"
        "opus-1.5.2/silk/HP_variable_cutoff.c"
        "opus-1.5.2/silk/init_decoder.c"
        "opus-1.5.2/silk/init_encoder.c"
        "opus-1.5.2/silk/inner_prod_aligned.c"
        "opus-1.5.2/silk/interpolate.c"
        "opus-1.5.2/silk/lin2log.c"
        "opus-1.5.2/silk/log2lin.c"
        "opus-1.5.2/silk/LP_variable_cutoff.c"
        "opus-1.5.2/silk/LPC_analysis_filter.c"
        "opus-1.5.2/silk/LPC_fit.c"
        "opus-1.5.2/silk/LPC_inv_pred_gain.c"
        "opus-1.5.2/silk/NLSF_decode.c"
        "opus-1.5.2/silk/NLSF_del_dec_quant.c"
        "opus-1.5.2/silk/NLSF_encode.c"
        "opus-1.5.2/silk/NLSF_stabilize.c"
        "opus-1.5.2/silk/NLSF_unpack.c"
        "opus-1.5.2/silk/NLSF_VQ_weights_laroia.c"
        "opus-1.5.2/silk/NLSF_VQ.c"
        "opus-1.5.2/silk/NLSF2A.c"
        "opus-1.5.2/silk/NSQ_del_dec.c"
        "opus-1.5.2/silk/NSQ.c"
        "opus-1.5.2/silk/pitch_est_tables.c"
        "opus-1.5.2/silk/PLC.c"
        "opus-1.5.2/silk/process_NLSFs.c"
        "opus-1.5.2/silk/quant_LTP_gains.c"
        "opus-1.5.2/silk/resampler_down2_3.c"
        "opus-1.5.2/silk/resampler_down2.c"
        "opus-1.5.2/silk/resampler_private_AR2.c"
        "opus-1.5.2/silk/resampler_private_down_FIR.c"
        "opus-1.5.2/silk/resampler_private_IIR_FIR.c"
        "opus-1.5.2/silk/resampler_private_up2_HQ.c"
        "opus-1.5.2/silk/resampler_rom.c"
        "opus-1.5.2/silk/resampler.c"
        "opus-1.5.2/silk/shell_coder.c"
        "opus-1.5.2/silk/sigm_Q15.c"
        "opus-1.5.2/silk/sort.c"
        "opus-1.5.2/silk/stereo_decode_pred.c"
        "opus-1.5.2/silk/stereo_encode_pred.c"
        "opus-1.5.2/silk/stereo_find_predictor.c"
        "opus-1.5.2/silk/stereo_LR_to_MS.c"
        "opus-1.5.2/silk/stereo_MS_to_LR.c"
        "opus-1.5.2/silk/stereo_quant_pred.c"
        "opus-1.5.2/silk/sum_sqr_shift.c"
        "opus-1.5.2/silk/table_LSF_cos.c"
        "opus-1.5.2/silk/tables_gain.c"
        "opus-1.5.2/silk/tables_LTP.c"
        "opus-1.5.2/silk/tables_NLSF_CB_NB_MB.c"
        "opus-1.5.2/silk/tables_NLSF_CB_WB.c"
        "opus-1.5.2/silk/tables_other.c"
        "opus-1.5.2/silk/tables_pitch_lag.c"
        "opus-1.5.2/silk/tables_pulses_per_block.c"
        "opus-1.5.2/silk/VAD.c"
        "opus-1.5.2/silk/VQ_WMat_EC.c"
        "opus-1.5.2/src/extensions.c"
        "opus-1.5.2/src/mapping_matrix.c"
        "opus-1.5.2/src/opus_decoder.c"
        "opus-1.5.2/src/opus_encoder.c"
        "opus-1.5.2/src/opus.c"
        "opus-1.5.2/src/repacketizer.c"

    INCLUDE_DIRS
        # The vendored layout mirrors upstream autotools, which compiles from
        # the project root and lets every source's `#include "config.h"` /
        # `#include "arch.h"` / `#include "main_FIX.h"` resolve from there.
        # We list the project root FIRST so the build-system-generated
        # config.h is found by every translation unit, then expose include/,
        # celt/, silk/, silk/fixed/ for the rest of the headers.
        "opus-1.5.2"
        "opus-1.5.2/include"
        "opus-1.5.2/celt"
        "opus-1.5.2/silk"
        "opus-1.5.2/silk/fixed"

    REQUIRES
        # No managed-component deps. libm is provided by newlib (component 'm'
        # in the esp32s3 target) which IDF links automatically.
)

# Compile-time feature flags. These MUST match components/opus/opus-1.5.2/config.h
# (which is #include'd as "config.h" wherever the source does #ifdef HAVE_CONFIG_H).
#
# -DHAVE_CONFIG_H        : every source does #ifdef HAVE_CONFIG_H / #include "config.h"
# -DOPUS_BUILD           : marks symbols as hidden (fvisibility=hidden); without it
#                          the public API gets default ELF visibility, but the
#                          headers rely on OPUS_BUILD to expand to the
#                          __attribute__((visibility("default"))) on api-export
#                          functions. Belt and suspenders.
# -DFIXED_POINT          : the silk/ and celt/ headers take different code paths
#                          under FIXED_POINT (and match our config.h define).
# -DOPUS_FIXED_POINT     : same flag by the historical alternate name (the
#                          silk sources check both).
# -DHAVE_LRINTF=1        : the encoder's resampler references lrintf; defining
#                          it on the command line is more reliable than relying
#                          on config.h being included before that point in
#                          every translation unit.
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    HAVE_CONFIG_H
    OPUS_BUILD
    FIXED_POINT
    OPUS_FIXED_POINT
    HAVE_LRINT=1
    HAVE_LRINTF=1
    HAVE_STDINT_H=1
    HAVE_INTTYPES_H=1
    HAVE_STDIO_H=1
    VAR_ARRAYS=1
    DISABLE_FLOAT_API=1
)

# Quiet a few upstream warnings that aren't actionable in a vendored third-party:
#   -Wno-unused-parameter   : upstream uses CELT_BUILD guards and (void) casts
#                             inconsistently; not worth patching.
#   -Wno-misleading-indentation : the fixed-point layer has deep if/else trees
#                                 that clang-format differently than upstream.
#   -Wno-stringop-overflow  : false positives from GCC 12.x's static analyzer
#                             in SILK's `silk_HP_variable_cutoff(psEnc->state_Fxx)`
#                             call — the function takes an unsized-array parameter
#                             (silk_encoder_state_Fxx[]) and the analyzer can't
#                             see the underlying size of state_Fxx at the call
#                             site, so it warns "accessing 8632 bytes in a region
#                             of size 8". The code is correct (state_Fxx is a
#                             [ENCODER_NUM_CHANNELS] array), and the warning
#                             fires on 5.1.6's GCC 12.2.0 but not 6.0.1's newer
#                             toolchain. GCC 13+ may regress again.
target_compile_options(${COMPONENT_LIB} PRIVATE
    -Wno-unused-parameter
    -Wno-misleading-indentation
    -Wno-unused-but-set-variable
    -Wno-stringop-overflow
)
