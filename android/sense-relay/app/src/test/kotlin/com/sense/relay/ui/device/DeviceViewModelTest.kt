package com.sense.relay.ui.device

import com.sense.relay.core.model.ApiError
import com.sense.relay.core.result.Outcome
import com.sense.relay.data.StatusRepository
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayConnectionState
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.RelayState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

/**
 * Pins [DeviceViewModel]: it independently carries the relay state and the
 * polled server status. Both sources are StateFlow/MutableStateFlow (emit
 * immediately), so the test runs on virtual time with a collecting
 * subscriber (the Phase-4 `HomeViewModelTest` pattern — `stateIn`
 * `WhileSubscribed` only collects the upstream while someone is subscribed,
 * so the test holds a collector in `backgroundScope`). The controller is
 * reset between tests so the singleton doesn't bleed.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class DeviceViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest
    fun setUp() {
        Dispatchers.setMain(dispatcher)
        RelayController.reset()
    }

    @AfterTest
    fun tearDown() = Dispatchers.resetMain()

    private class FakeStatusRepository(initial: Outcome<ServerStatus>) : StatusRepository {
        val flow = MutableStateFlow(initial)
        override fun observeStatus(): Flow<Outcome<ServerStatus>> = flow
    }

    private fun status(active: Int) = ServerStatus(
        reachable = true, authenticated = true, version = "0.1.0",
        uptimeSeconds = 1, activeSessions = active, totalSessions = 2, recentEvents24h = 3,
    )

    /** Subscribe so `stateIn(WhileSubscribed)` collects the upstream. */
    private fun subscribe(scope: CoroutineScope, vm: DeviceViewModel) {
        scope.launch { vm.state.toList(mutableListOf()) }
    }

    @Test fun initialStateCarriesRelayInitialAndLoadingServer() = runTest(dispatcher) {
        val vm = DeviceViewModel(
            RelayController,
            FakeStatusRepository(Outcome.Failure(ApiError.Unreachable("loading"))),
        )
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()
        val s = vm.state.value
        assertEquals(RelayState.Initial, s.relay)
        assertIs<Outcome.Failure>(s.server)
    }

    @Test fun statusEmissionUpdatesServerWhileRelayUnchanged() = runTest(dispatcher) {
        val status = FakeStatusRepository(Outcome.Failure(ApiError.Unreachable("loading")))
        val vm = DeviceViewModel(RelayController, status)
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()

        status.flow.value = Outcome.Success(status(5))
        testScheduler.advanceUntilIdle()
        val s = vm.state.filter { it.server is Outcome.Success }.first()
        assertEquals(RelayState.Initial, s.relay, "relay unchanged")
        val success = assertIs<Outcome.Success<ServerStatus>>(s.server)
        assertEquals(5, success.value.activeSessions)
    }

    @Test fun relayChangeUpdatesRelayWhileServerUnchanged() = runTest(dispatcher) {
        val status = FakeStatusRepository(Outcome.Success(status(1)))
        val vm = DeviceViewModel(RelayController, status)
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()
        // Confirm the server is present first.
        vm.state.filter { it.server is Outcome.Success }.first()

        RelayController.updateDevice(DeviceState.Connected("AA:BB", "sense-1"))
        RelayController.updateConnection(RelayConnectionState.Live("s1", 0))
        testScheduler.advanceUntilIdle()
        val s = vm.state.filter { it.relay.device is DeviceState.Connected }.first()
        val dev = assertIs<DeviceState.Connected>(s.relay.device)
        assertEquals("AA:BB", dev.address)
        val conn = assertIs<RelayConnectionState.Live>(s.relay.connection)
        assertEquals("s1", conn.sessionId)
        // Server unchanged.
        assertIs<Outcome.Success<ServerStatus>>(s.server)
    }

    @Test fun bothPresentAreCarriedTogether() = runTest(dispatcher) {
        val status = FakeStatusRepository(Outcome.Success(status(7)))
        val vm = DeviceViewModel(RelayController, status)
        subscribe(backgroundScope, vm)
        RelayController.updateConnection(RelayConnectionState.Live("sx", 100))
        testScheduler.advanceUntilIdle()
        val s = vm.state.filter {
            it.relay.connection is RelayConnectionState.Live && it.server is Outcome.Success
        }.first()
        assertEquals("sx", (s.relay.connection as RelayConnectionState.Live).sessionId)
        assertEquals(7, (s.server as Outcome.Success).value.activeSessions)
    }
}