package com.opensapien.relay.data

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.domain.model.ServerStatus
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Pins [PollingStatusRepository]'s contract without any real network or
 * wall-clock:
 *   - polls on `ON_START`, immediately caching the first result;
 *   - waits exactly [PollingStatusRepository.DEFAULT_INTERVAL_MS] between polls;
 *   - stops polling on `ON_STOP` and resumes on the next `ON_START`;
 *   - surfaces a fetch [Outcome.Failure] as the cached value without
 *     killing the loop;
 *   - hands a new subscriber the latest cached value on subscribe.
 *
 * The loop runs on the test scheduler (via `backgroundScope`) with a
 * `delayFn` that records the requested durations, so cadence is asserted
 * on virtual time. The lifecycle is a [LifecycleRegistry.createUnsafe] so
 * the main-thread check doesn't apply in a host unit test.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class PollingStatusRepositoryTest {

    private class FakeOwner : LifecycleOwner {
        val registry = LifecycleRegistry.createUnsafe(this)
        override val lifecycle: Lifecycle get() = registry
    }

    private fun status(active: Int) = ServerStatus(
        reachable = true,
        authenticated = true,
        version = "0.1.0",
        uptimeSeconds = 10,
        activeSessions = active,
        totalSessions = 5,
        recentEvents24h = 7,
    )

    @Test fun pollsImmediatelyOnStart() = runTest {
        val owner = FakeOwner()
        val repo = PollingStatusRepository(
            fetch = { Outcome.Success(status(1)) },
            lifecycle = owner.lifecycle,
            delayFn = { delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()

        val out = assertIs<Outcome.Success<ServerStatus>>(repo.latest)
        assertEquals(status(1), out.value)
    }

    @Test fun waitsTheDefaultIntervalBetweenPolls() = runTest {
        val owner = FakeOwner()
        val recorded = mutableListOf<Long>()
        var count = 0
        val repo = PollingStatusRepository(
            fetch = { count++; Outcome.Success(status(count)) },
            lifecycle = owner.lifecycle,
            delayFn = { recorded.add(it); delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()
        assertEquals(1, count, "first poll should fire immediately on start")

        testScheduler.advanceTimeBy(PollingStatusRepository.DEFAULT_INTERVAL_MS + 1)
        testScheduler.runCurrent()
        assertEquals(2, count, "second poll after one interval")

        // Every recorded delay is exactly the 2s cadence.
        assertTrue(recorded.isNotEmpty())
        assertTrue(recorded.all { it == PollingStatusRepository.DEFAULT_INTERVAL_MS },
            "expected all delays == 2000ms, was $recorded")
    }

    @Test fun stopsOnStopAndResumesOnStart() = runTest {
        val owner = FakeOwner()
        var count = 0
        val repo = PollingStatusRepository(
            fetch = { count++; Outcome.Success(status(count)) },
            lifecycle = owner.lifecycle,
            delayFn = { delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()
        testScheduler.advanceTimeBy(PollingStatusRepository.DEFAULT_INTERVAL_MS + 1)
        testScheduler.runCurrent()
        val afterTwo = count
        assertTrue(afterTwo >= 2)

        // Background: polling must pause.
        owner.registry.currentState = Lifecycle.State.CREATED
        testScheduler.advanceTimeBy(10 * PollingStatusRepository.DEFAULT_INTERVAL_MS)
        testScheduler.runCurrent()
        assertEquals(afterTwo, count, "no polls while stopped")

        // Foreground again: polling resumes with a fresh immediate poll.
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()
        assertEquals(afterTwo + 1, count, "resumes with an immediate poll on restart")
    }

    @Test fun failureIsCachedAndLoopSurvives() = runTest {
        val owner = FakeOwner()
        var count = 0
        val repo = PollingStatusRepository(
            fetch = {
                count++
                if (count == 1) {
                    Outcome.Failure(com.opensapien.relay.core.model.ApiError.Unreachable("down"))
                } else {
                    Outcome.Success(status(count))
                }
            },
            lifecycle = owner.lifecycle,
            delayFn = { delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()
        assertIs<Outcome.Failure>(repo.latest)

        // The failure did not tear down the loop: the next interval polls again.
        testScheduler.advanceTimeBy(PollingStatusRepository.DEFAULT_INTERVAL_MS + 1)
        testScheduler.runCurrent()
        assertIs<Outcome.Success<ServerStatus>>(repo.latest)
    }

    @Test fun loopSurvivesAThrowingFetch() = runTest {
        // A `fetch` is contracted to return an Outcome, not throw — but if
        // one ever does (e.g. the production fetch's DataStore read blew up
        // before its own guard), the poll loop must NOT die (that would
        // crash the app under the SupervisorJob). The first poll throws; the
        // loop swallows it, keeps the cache untouched, and the next tick
        // polls again successfully.
        val owner = FakeOwner()
        var count = 0
        val repo = PollingStatusRepository(
            fetch = {
                count++
                if (count == 1) throw IOException("boom") else Outcome.Success(status(count))
            },
            lifecycle = owner.lifecycle,
            delayFn = { delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()
        assertEquals(1, count, "first poll ran and threw")
        assertNull(repo.latest, "a thrown fetch leaves the cache untouched")

        testScheduler.advanceTimeBy(PollingStatusRepository.DEFAULT_INTERVAL_MS + 1)
        testScheduler.runCurrent()
        assertEquals(2, count, "loop survived the throw and polled again")
        assertIs<Outcome.Success<ServerStatus>>(repo.latest)
    }

    @Test fun observeStatusEmitsCachedValueToSubscriber() = runTest {
        val owner = FakeOwner()
        val repo = PollingStatusRepository(
            fetch = { Outcome.Success(status(3)) },
            lifecycle = owner.lifecycle,
            delayFn = { delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()

        // A brand-new subscriber gets the latest cached value immediately.
        val emitted = repo.observeStatus().first()
        val success = assertIs<Outcome.Success<ServerStatus>>(emitted)
        assertEquals(status(3), success.value)
    }

    @Test fun refreshFetchesImmediatelyOutOfCycle() = runTest {
        // The poll loop has not advanced (no testScheduler advance), so without
        // a refresh the cache would still hold the first poll's value. A
        // refresh() must run one fetch right now and publish the new value.
        val owner = FakeOwner()
        var count = 0
        val repo = PollingStatusRepository(
            fetch = { count++; Outcome.Success(status(count)) },
            lifecycle = owner.lifecycle,
            delayFn = { delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()
        assertEquals(1, count, "first poll on start")
        val before = (repo.latest as Outcome.Success).value.activeSessions

        repo.refresh()  // out-of-cycle fetch, no advanceTimeBy
        testScheduler.runCurrent()

        assertEquals(2, count, "refresh() triggered an extra fetch immediately")
        val after = (repo.latest as Outcome.Success).value.activeSessions
        assertEquals(2, after, "the refresh's result was published")
        assertTrue(after != before)
    }

    @Test fun refreshSwallowsAThrowingFetchAndKeepsCache() = runTest {
        val owner = FakeOwner()
        var count = 0
        val repo = PollingStatusRepository(
            fetch = {
                count++
                if (count == 2) throw IOException("boom")
                Outcome.Success(status(count))
            },
            lifecycle = owner.lifecycle,
            delayFn = { delay(it) },
            scope = backgroundScope,
        )
        owner.registry.currentState = Lifecycle.State.STARTED
        testScheduler.runCurrent()
        val first = repo.latest
        assertIs<Outcome.Success<ServerStatus>>(first)

        repo.refresh()  // the 2nd fetch throws
        testScheduler.runCurrent()

        assertEquals(first, repo.latest, "a thrown refresh leaves the cache as-is")
        assertIs<Outcome.Success<ServerStatus>>(repo.latest)
    }
}
