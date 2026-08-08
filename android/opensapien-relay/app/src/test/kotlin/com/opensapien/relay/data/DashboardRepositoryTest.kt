package com.opensapien.relay.data

import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.domain.model.ServerStatus
import com.opensapien.relay.domain.model.SessionId
import com.opensapien.relay.domain.model.SessionSummary
import com.opensapien.relay.relay.DeviceState
import com.opensapien.relay.relay.RelayController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import java.time.Instant
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * Pins [DashboardRepositoryImpl]: it fans the relay controller, the status
 * poller, and the session list into one [DashboardState]. Reactivity is
 * asserted on real dispatchers with a short real-time timeout — the same
 * pattern as [DeviceRepositoryTest] — because `combine`/`stateIn` over
 * `StateFlow` sources don't cooperate with `TestDispatcher` in coroutines
 * 1.9.0.
 */
class DashboardRepositoryTest {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @AfterTest fun tearDown() {
        scope.cancel()
        RelayController.reset()
    }

    private class FakeStatusRepository(initial: Outcome<ServerStatus>) : StatusRepository {
        val flow = MutableStateFlow(initial)
        var refreshCount = 0
        override fun observeStatus(): Flow<Outcome<ServerStatus>> = flow
        override suspend fun refresh() { refreshCount++ }
    }

    private fun status(active: Int) = ServerStatus(
        reachable = true, authenticated = true, version = "0.1.0",
        uptimeSeconds = 1, activeSessions = active, totalSessions = 2, recentEvents24h = 3,
    )

    private fun summary(id: String) = SessionSummary(
        id = SessionId(id),
        startedAt = Instant.EPOCH,
        endedAt = null,
        durationMs = 1000,
        transcriptCount = 1,
        preview = "preview-$id",
    )

    @Test fun initialValueIsLoadingBeforeSubscription() {
        RelayController.reset()
        val repo = DashboardRepositoryImpl(
            RelayController,
            FakeStatusRepository(Outcome.Success(status(1))),
            FakeSessionRepository(),
            scope,
        )
        // No collector yet → WhileSubscribed hasn't started the upstream.
        assertEquals(DashboardState.Loading, repo.observe().value)
    }

    @Test fun loadedOnceAllSourcesHaveAValue() = runBlocking {
        RelayController.reset()
        val repo = DashboardRepositoryImpl(
            RelayController,
            FakeStatusRepository(Outcome.Success(status(4))),
            FakeSessionRepository(),
            scope,
        )
        val loaded = withTimeout(2000) {
            repo.observe().filter { it is DashboardState.Loaded }.first()
        }
        val l = assertIs<DashboardState.Loaded>(loaded)
        assertEquals(Outcome.Success(status(4)), l.server)
    }

    @Test fun recentSessionsAreCappedAtThree() = runBlocking {
        RelayController.reset()
        val session = FakeSessionRepository()
        session.queue(
            items = (1..10).map { summary("s$it") },
            nextCursor = null,
        )
        val repo = DashboardRepositoryImpl(
            RelayController,
            FakeStatusRepository(Outcome.Success(status(1))),
            session,
            scope,
        )
        // The session flow retains its latest Page value, so the dashboard
        // picks it up as soon as it subscribes.
        session.loadMoreSessions()
        val loaded = withTimeout(2000) {
            repo.observe().filter {
                it is DashboardState.Loaded && it.recentSessions.isNotEmpty()
            }.first()
        }
        val l = assertIs<DashboardState.Loaded>(loaded)
        assertEquals(3, l.recentSessions.size)
        assertEquals(listOf("s1", "s2", "s3"), l.recentSessions.map { it.id.value })
    }

    @Test fun relayDeviceChangeReEmits() = runBlocking {
        RelayController.reset()
        val repo = DashboardRepositoryImpl(
            RelayController,
            FakeStatusRepository(Outcome.Success(status(1))),
            FakeSessionRepository(),
            scope,
        )
        // Wait for the first Loaded, then flip the device.
        withTimeout(2000) { repo.observe().filter { it is DashboardState.Loaded }.first() }
        RelayController.updateDevice(DeviceState.Connected("AA:BB", "sense-1"))
        val connected = withTimeout(2000) {
            repo.observe().filter {
                it is DashboardState.Loaded && it.relay.device is DeviceState.Connected
            }.first()
        }
        val l = assertIs<DashboardState.Loaded>(connected)
        val dev = assertIs<DeviceState.Connected>(l.relay.device)
        assertEquals("AA:BB", dev.address)
    }

    @Test fun statusChangeReEmits() = runBlocking {
        RelayController.reset()
        val fakeStatus = FakeStatusRepository(Outcome.Success(status(1)))
        val repo = DashboardRepositoryImpl(
            RelayController,
            fakeStatus,
            FakeSessionRepository(),
            scope,
        )
        withTimeout(2000) {
            repo.observe().filter {
                it is DashboardState.Loaded &&
                    (it.server as? Outcome.Success)?.value?.activeSessions == 1
            }.first()
        }
        fakeStatus.flow.value = Outcome.Success(status(9))
        val updated = withTimeout(2000) {
            repo.observe().filter {
                it is DashboardState.Loaded &&
                    (it.server as? Outcome.Success)?.value?.activeSessions == 9
            }.first()
        }
        val l = assertIs<DashboardState.Loaded>(updated)
        assertTrue((l.server as Outcome.Success).value.activeSessions == 9)
    }

    @Test fun refreshDelegatesToBothChildren() = runBlocking {
        // A pull-to-refresh on Home fans out: one immediate status poll AND a
        // reset-to-page-1 of the session list. Both children's refresh must
        // be invoked exactly once.
        RelayController.reset()
        val fakeStatus = FakeStatusRepository(Outcome.Success(status(1)))
        val fakeSession = FakeSessionRepository()
        val repo = DashboardRepositoryImpl(RelayController, fakeStatus, fakeSession, scope)
        // Let the combined flow go Loaded first so we're past the seed.
        withTimeout(2000) { repo.observe().filter { it is DashboardState.Loaded }.first() }

        repo.refresh()

        assertEquals(1, fakeStatus.refreshCount, "status refreshed once")
        assertEquals(1, fakeSession.refreshCount, "sessions refreshed once")
    }
}
