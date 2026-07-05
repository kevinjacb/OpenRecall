package com.sense.relay.data

import com.sense.relay.domain.model.DeviceSummary
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.RelayState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
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
     * [RelayController] state transitions as they happen.
     *
     * The returned [StateFlow] is reactive: it is backed by a
     * collector that watches [RelayController.state] and pushes the
     * mapped [DeviceSummary] into the repository's internal
     * [MutableStateFlow]. A subscriber that holds the returned
     * [StateFlow] will receive a new value every time the
     * [RelayController] state changes (or [RelayController.requestRefresh]
     * bumps the revision), without needing to re-call this method.
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
 * Reactive bridge: the implementation owns a [CoroutineScope]
 * tied to the process (NOT created per call) and a long-running
 * collector that maps [RelayController.state] into the local
 * [MutableStateFlow]. The collector runs on [Dispatchers.Default]
 * so it does not block the main thread; its [SupervisorJob] means
 * a single failure in the map step will not tear down the
 * collector. The local [MutableStateFlow] is exposed read-only via
 * [asStateFlow], so subscribers cannot mutate it.
 *
 * The `lastSeen` field is captured by *us* (we re-emit on every
 * controller update), not by the controller itself. That keeps
 * `RelayState` clean of UI-flavored timestamps.
 *
 * **Why a process-wide scope (and not, e.g., one created per
 * call):** creating a new scope per call would leak a coroutine
 * and a job for every UI recomposition, every config change, and
 * every test invocation. The process-wide scope is created once
 * per [DeviceRepositoryImpl] instance and lives for as long as
 * the instance is reachable; the production code constructs it
 * once in [com.sense.relay.data.RepositoryModule.init].
 *
 * **Test note:** the collector runs on a real dispatcher, so tests
 * that want to observe a propagation after a controller update
 * need a small real-time wait (`Thread.sleep` or `withTimeout`
 * over a `runBlocking` block) — the `kotlinx.coroutines.test`
 * `runTest` scheduler does not advance the production scope.
 */
class DeviceRepositoryImpl(
    private val controller: RelayController,
) : DeviceRepository {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private val state = MutableStateFlow(
        DeviceSummary(
            address = null,
            name = null,
            lastSeen = null,
        ),
    )

    init {
        // Bridge the controller's state into our StateFlow. The
        // collector is launched once and runs for the lifetime of
        // the process; it pushes every mapped DeviceSummary into
        // `state`. New emissions overwrite the previous one even
        // if they're equals-equal, so subscribers see the fresh
        // `lastSeen` timestamp.
        scope.launch {
            controller.state.collect { relayState ->
                state.value = relayState.toDeviceSummary()
            }
        }
    }

    override fun observeDevice(): StateFlow<DeviceSummary> = state.asStateFlow()

    /**
     * Reactive bridge for callers that want a `Flow` (not a
     * snapshot) over the controller's state. Returns a `Flow` that
     * emits the latest [DeviceSummary] on every controller update.
     *
     * This is a convenience mapping — [observeDevice] is now also
     * reactive (see the [DeviceRepository.observeDevice] KDoc), so
     * most callers should use that. This method is preserved for
     * callers that want a cold `Flow` (e.g. tests, or compose
     * collectAsState patterns that prefer a Flow signature).
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
