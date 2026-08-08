package com.openrecall.relay.domain.model

import java.time.Instant

/**
 * What the relay knows about the wearable right now (`GET /device/status`).
 *
 * The payload is honest about a split, and the UI has to preserve it:
 *
 * * [batteryPct], [storageFreeBytes] and [firmwareVersion] are **not
 *   measured**. Battery sensing does not exist at any layer of this product —
 *   no ADC channel, no fuel gauge, no telemetry frame — so the server ships a
 *   fixed placeholder. [measured] is false in that case and the value must be
 *   rendered visibly provisional. A plausible-looking 45 % is worse than a
 *   blank if anyone ever acts on it.
 * * Everything from [recording] down is genuinely measured from the gateway.
 *
 * [lastPacketAgeS] is a real heartbeat rather than a speech detector: the
 * firmware keeps emitting gap-marker packets while its VAD suppresses
 * silence, so packets flow whether or not anyone is talking. Paired with
 * [lastTranscriptAgeS] it separates the three states a user cares about —
 * alive and hearing speech, alive in a quiet room, and gone.
 */
data class DeviceStatus(
    val measured: Boolean,
    val batteryPct: Double?,
    val storageFreeBytes: Long?,
    val firmwareVersion: String?,
    val recording: Boolean,
    val relayConnected: Boolean,
    val microphoneAvailable: Boolean,
    val cameraAvailable: Boolean,
    val lastPacketAt: Instant?,
    val lastPacketAgeS: Double?,
    val lastTranscriptAt: Instant?,
    val lastTranscriptAgeS: Double?,
) {
    /** True when a packet arrived recently enough that the link is live. */
    fun isFresh(thresholdS: Double = FRESH_THRESHOLD_S): Boolean =
        lastPacketAgeS != null && lastPacketAgeS <= thresholdS

    companion object {
        /**
         * Gap markers flow continuously while the device is awake, so a link
         * that has produced nothing for a minute has stopped talking to us.
         */
        const val FRESH_THRESHOLD_S = 60.0
    }
}
