package com.sense.relay.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

@OptIn(ExperimentalCoroutinesApi::class)
class CommandsViewModelPollingTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `startPolling calls refresh on the first dispatch and again every interval`() =
        runTest(dispatcher) {
            val counter = AtomicInteger(0)
            val repo = CountingRepo(counter)
            val vm = CommandsViewModel(repo, pollDispatcher = dispatcher)

            try {
                vm.startPolling(intervalMs = 50L)
                // Bounded advance. With StandardTestDispatcher the launch body
                // is queued and runs when the scheduler processes the T=0
                // event. advanceTimeBy(N) covers events at T=0, 50, 100, ...,
                // up to but not including T=N. So advanceTimeBy(200L) runs
                // 4 events: 0, 50, 100, 150 = 4 refreshes.
                advanceTimeBy(200L)
                assertEquals(4, counter.get())

                // Advancing another 200ms runs 4 more: 200, 250, 300, 350 = 8.
                advanceTimeBy(200L)
                assertEquals(8, counter.get())
            } finally {
                vm.stopPolling()
            }
        }

    @Test
    fun `startPolling called twice does not stack timers`() = runTest(dispatcher) {
        val counter = AtomicInteger(0)
        val repo = CountingRepo(counter)
        val vm = CommandsViewModel(repo, pollDispatcher = dispatcher)

        try {
            vm.startPolling(intervalMs = 50L)
            vm.startPolling(intervalMs = 50L) // second call cancels the first
            advanceTimeBy(200L)
            // 4 events: 0, 50, 100, 150 — NOT 8 from two stacked timers.
            assertEquals(4, counter.get())
        } finally {
            vm.stopPolling()
        }
    }

    @Test
    fun `stopPolling cancels subsequent ticks`() = runTest(dispatcher) {
        val counter = AtomicInteger(0)
        val repo = CountingRepo(counter)
        val vm = CommandsViewModel(repo, pollDispatcher = dispatcher)

        try {
            vm.startPolling(intervalMs = 50L)
            advanceTimeBy(100L) // 2 events: 0, 50 = 2
            assertEquals(2, counter.get())

            vm.stopPolling()
            advanceTimeBy(500L) // no growth
            assertEquals(2, counter.get())
        } finally {
            vm.stopPolling()
        }
    }

    @Test
    fun `stopPolling on a non-polling ViewModel is a no-op`() = runTest(dispatcher) {
        val repo = CountingRepo(AtomicInteger(0))
        val vm = CommandsViewModel(repo, pollDispatcher = dispatcher)
        vm.stopPolling() // should not throw
        advanceTimeBy(500L) // no tick ever fires
        assertEquals(0, repo.refreshCount.get())
    }

    @Test
    fun `refresh failures do not stop the polling loop`() = runTest(dispatcher) {
        val counter = AtomicInteger(0)
        val repo = FlakyRepo(counter, failOnCalls = setOf(1))
        val vm = CommandsViewModel(repo, pollDispatcher = dispatcher)

        try {
            vm.startPolling(intervalMs = 50L)
            // First event (T=0) fails -> UiState.Error; loop continues.
            // Second event (T=50) succeeds -> UiState.Ready.
            advanceTimeBy(100L)
            assertTrue(vm.state.value is CommandsViewModel.UiState.Ready)
            // We made it past the first failure, so the loop is alive.
            assertEquals(2, counter.get())
        } finally {
            vm.stopPolling()
        }
    }

    // -- test doubles --

    private class CountingRepo(val refreshCount: AtomicInteger) :
        CommandRepository(StubCommandApi()) {
        override suspend fun listActive(): List<Command> {
            refreshCount.incrementAndGet()
            return emptyList()
        }
    }

    private class FlakyRepo(
        val refreshCount: AtomicInteger,
        private val failOnCalls: Set<Int>,
    ) : CommandRepository(StubCommandApi()) {
        override suspend fun listActive(): List<Command> {
            val n = refreshCount.incrementAndGet()
            if (n in failOnCalls) throw RuntimeException("network error")
            return emptyList()
        }
    }
}
