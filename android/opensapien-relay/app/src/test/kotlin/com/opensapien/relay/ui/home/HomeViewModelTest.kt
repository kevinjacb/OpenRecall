package com.opensapien.relay.ui.home

import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.data.DashboardRepository
import com.opensapien.relay.data.DashboardState
import com.opensapien.relay.domain.model.ServerStatus
import com.opensapien.relay.relay.RelayState
import com.opensapien.relay.relay.RelayStarter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
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
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * Pins [HomeViewModel]'s 1:1 mapping from [DashboardState] to
 * [HomeUiState]. The ViewModel re-hosts the repository flow on
 * `viewModelScope` (Main), so the test swaps Main for a
 * [StandardTestDispatcher] sharing the `runTest` scheduler, and drives
 * the mapping by pushing states through a fake repository.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class HomeViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest fun setUp() = Dispatchers.setMain(dispatcher)

    @AfterTest fun tearDown() = Dispatchers.resetMain()

    private class FakeDashboardRepository(
        val flow: MutableStateFlow<DashboardState>,
    ) : DashboardRepository {
        var refreshCount = 0
        override fun observe(): StateFlow<DashboardState> = flow
        override suspend fun refresh() { refreshCount++ }
    }

    private class FakeRelayStarter : RelayStarter {
        var startCount = 0
        override fun start() { startCount++ }
    }

    private fun loaded() = DashboardState.Loaded(
        relay = RelayState.Initial,
        server = Outcome.Success(
            ServerStatus(
                reachable = true, authenticated = true, version = "0.1.0",
                uptimeSeconds = 1, activeSessions = 0, totalSessions = 0, recentEvents24h = 0,
            ),
        ),
        recentSessions = emptyList(),
    )

    @Test fun initialStateIsLoading() = runTest(dispatcher) {
        val repo = FakeDashboardRepository(MutableStateFlow(DashboardState.Loading))
        val vm = HomeViewModel(repo, FakeRelayStarter())
        // Before anyone collects, the stateIn seed is Loading.
        assertEquals(HomeUiState.Loading, vm.state.value)
    }

    @Test fun mapsLoadedFromRepository() = runTest(dispatcher) {
        val flow = MutableStateFlow<DashboardState>(DashboardState.Loading)
        val vm = HomeViewModel(FakeDashboardRepository(flow), FakeRelayStarter())
        val seen = mutableListOf<HomeUiState>()
        val job = backgroundScope.launch { vm.state.toList(seen) }

        flow.value = loaded()
        testScheduler.advanceUntilIdle()

        val ui = assertIs<HomeUiState.Loaded>(vm.state.value)
        assertEquals(loaded(), ui.dashboard)
        job.cancel()
    }

    @Test fun mapsFailedFromRepository() = runTest(dispatcher) {
        val flow = MutableStateFlow<DashboardState>(DashboardState.Loading)
        val vm = HomeViewModel(FakeDashboardRepository(flow), FakeRelayStarter())
        val job = backgroundScope.launch { vm.state.toList(mutableListOf()) }

        flow.value = DashboardState.Failed("boom")
        testScheduler.advanceUntilIdle()

        val ui = assertIs<HomeUiState.Failed>(vm.state.value)
        assertEquals("boom", ui.reason)
        job.cancel()
    }

    @Test fun onRefreshCallsDashboardRefreshAndTogglesIsRefreshing() = runTest(dispatcher) {
        val repo = FakeDashboardRepository(MutableStateFlow(DashboardState.Loading))
        val vm = HomeViewModel(repo, FakeRelayStarter())
        assertFalse(vm.isRefreshing.value, "not refreshing before the gesture")

        vm.onRefresh()
        testScheduler.advanceUntilIdle()

        assertEquals(1, repo.refreshCount, "dashboard.refresh() invoked once")
        assertFalse(vm.isRefreshing.value, "isRefreshing cleared after the refresh completes")
    }

    @Test fun onRetryConnectionCallsRelayStarter() = runTest(dispatcher) {
        val starter = FakeRelayStarter()
        val vm = HomeViewModel(FakeDashboardRepository(MutableStateFlow(DashboardState.Loading)), starter)

        vm.onRetryConnection()

        assertEquals(1, starter.startCount, "relayStarter.start() invoked once")
    }
}
