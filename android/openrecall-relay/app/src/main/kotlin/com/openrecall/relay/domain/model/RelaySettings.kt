package com.openrecall.relay.domain.model

/**
 * The relay's durable settings document (`GET`/`PUT /settings`).
 *
 * These are **server-side desired state**, not phone preferences. That is the
 * whole reason the endpoint exists: commands are delivered only to a live
 * gateway session, so a command-only toggle would be dead whenever the device
 * is offline — which is most of the time. The server persists the desired
 * value, reconciles the device against it on every `hello`, and gates ingest
 * as a backstop.
 */
data class RelaySettings(
    val capture: CaptureSettings = CaptureSettings(),
    val retention: RetentionSettings = RetentionSettings(),
)

/**
 * The three capture toggles. They are not interchangeable:
 *
 * @param audioEnabled device-level. Desired state for the microphone; the
 *   server issues `start_audio`/`stop_audio` to converge the wearable on it.
 * @param saveAudio server-level. Controls only whether the frame log is
 *   written — the microphone keeps working and transcripts keep forming, so
 *   turning it off stops recordings being *replayable*, not being *heard*.
 * @param visionEnabled gates the vision pipeline.
 *
 * The design's third toggle was a wake word, which is cut: there is no
 * wake-word engine in the firmware and the wearable is designed without one.
 */
data class CaptureSettings(
    val audioEnabled: Boolean = true,
    val saveAudio: Boolean = true,
    val visionEnabled: Boolean = false,
)

/**
 * @param audioDays how long audio is kept. Transcripts, atoms and vectors are
 *   never swept — a deliberate tiered policy (audio is the bulky and most
 *   sensitive artifact; the derived text is the product), and one the UI has
 *   to state plainly because "the recording expires but the transcript does
 *   not" is not what a user assumes.
 */
data class RetentionSettings(
    val audioDays: Int = 30,
)

/**
 * Totals behind the Memories header (`GET /memory/stats`).
 *
 * [byKind] is also the kind vocabulary: the extractor emits free-form kinds,
 * so the filter chips are built from what actually exists rather than from a
 * hardcoded list that would silently hide everything else.
 */
data class MemoryStats(
    val total: Int = 0,
    val added24h: Int = 0,
    val byKind: Map<String, Int> = emptyMap(),
) {
    /** Kinds present in the store, most common first — the chip order. */
    val kinds: List<String>
        get() = byKind.entries.sortedWith(
            compareByDescending<Map.Entry<String, Int>> { it.value }.thenBy { it.key },
        ).map { it.key }
}
