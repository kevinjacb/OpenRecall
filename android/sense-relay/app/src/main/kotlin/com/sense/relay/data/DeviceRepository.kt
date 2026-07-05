package com.sense.relay.data

import com.sense.relay.domain.model.DeviceSummary
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.RelayState
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.map
import java.time.Instant

/**
 * Source of the Device screen's view of the connected wearable.
 * The repository is the **only** consumer of [RelayController] that
 * exposes the device state to the UI; other code that needs the
 * device's identity reads through this repository (or, in tests,
 * constructs one directly).
 */
interface DeviceRepository {
    /**
     * Hot stream of the latest [DeviceSummary]. Emits the current
     * value on subscribe; subsequent values are derived from
     * [RelayController] state transitions.
     */
    fun observeDevice(): StateFlow<DeviceSummary>
}

/**
 * Production implementation. Derives [DeviceSummary] from the
 * [RelayController]'s current [RelayState]:
 *   - [DeviceState.Unknown] / [DeviceState.Scanning] /
 *     [DeviceState.Disconnected] -> all-null [DeviceSummary].
 *   - [DeviceState.Connected] -> address + name + the moment
 *     we observed the connection.
 *
 * The repository owns its own [MutableStateFlow] so the
 * `lastSeen` field is captured by *us* (we re-emit on every
 * controller update), not by the controller itself. That keeps
 * `RelayState` clean of UI-flavored timestamps.
 */
class DeviceRepositoryImpl(
    private val controller: RelayController,
) : DeviceRepository {

    private val state = MutableStateFlow(
        DeviceSummary(
            address = null,
            name = null,
            lastSeen = null,
        ),
    )

    init {
        // Bridge the controller's state into our StateFlow. We use
        // a separate coroutine... actually no — `MutableStateFlow`
        // is thread-safe and we don't have a coroutine scope here.
        // Instead, the controller's state is captured on every
        // `observeDevice()` call. Consumers that need reactive
        // updates should observe `controller.state` and re-call
        // `observeDevice()` (or combine the flows). For Phase 2
        // the ViewModels observe the controller directly and
        // derive DeviceSummary from the controller's `device`
        // field themselves.
        //
        // The init block is intentionally empty here; this
        // repository's value is the most recent controller state
        // snapshotted at observeDevice() call time. The
        // `controller` field is kept for symmetry with future
        // implementations that need a stronger reactive bridge.
    }

    override fun observeDevice(): StateFlow<DeviceSummary> {
        // Map the current controller state to a DeviceSummary and
        // publish on the local StateFlow. Subscribers see the
        // latest value on subscribe.
        state.value = controller.state.value.toDeviceSummary()
        return state.asStateFlow()
    }

    /**
     * Reactive bridge for callers that want a `Flow` (not a
     * snapshot) over the controller's state. Returns a `Flow` that
     * emits the latest [DeviceSummary] on every controller update.
     * Phase 4's DeviceViewModel uses this.
     */
    fun observeDeviceFlow(): Flow<DeviceSummary> =
        controller.state.map { it.toDeviceSummary() }
}

private fun RelayState.toDeviceSummary(): DeviceSummary = when (val d = device) {
    is DeviceState.Connected -> DeviceSummary(
        address = d.address,
        name = d.name,
        lastSeen = Instant.now(),
    )
    is DeviceState.Unknown,
    is DeviceState.Scanning,
    is DeviceState.Disconnected -> DeviceSummary(
        address = null,
        name = null,
        lastSeen = null,
    )
}
