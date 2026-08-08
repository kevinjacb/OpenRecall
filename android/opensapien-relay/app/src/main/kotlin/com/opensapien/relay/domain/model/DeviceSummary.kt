package com.opensapien.relay.domain.model

import java.time.Instant

/**
 * The Device screen's view of the connected wearable. `address` and
 * `name` are nullable because the device may be unknown (no scan
 * result yet), scanning, or disconnected. `lastSeen` is the moment
 * the relay last got any signal from the device — null if never.
 *
 * The DeviceRepository (Phase 2) derives this from
 * `RelayController.state`; Phase 4's DeviceViewModel renders it.
 */
data class DeviceSummary(
    val address: String?,
    val name: String?,
    val lastSeen: Instant?,
)
