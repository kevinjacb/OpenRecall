package com.sense.relay.data

import com.sense.relay.core.model.ApiError
import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.ServerStatus
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * Pins the contract of the Phase 2 [StatusRepository] placeholder.
 * The interface ships now (so `RepositoryModule` and Phase 4
 * ViewModels compile); the real polling impl is Phase 4.
 *
 * What we pin here:
 *   - `observeStatus()` emits a single `Outcome.Failure` whose
 *     error is [ApiError.Unreachable] (so a screen can show a
 *     "server offline" affordance without crashing).
 *   - The failure reason mentions the phase that will wire it
 *     (greppable from logcat).
 *   - The interface compiles against the real [ServerStatus]
 *     domain type.
 */
class NoopStatusRepositoryTest {

    @Test fun emitsUnreachableFailureOnSubscribe() = runTest {
        val repo: StatusRepository = NoopStatusRepository()
        val out = repo.observeStatus().first()
        val failure = assertIs<Outcome.Failure>(out)
        val err = assertIs<ApiError.Unreachable>(failure.error)
        assertTrue(err.reason.contains("not yet wired"))
        val hasPhase = err.reason.contains("Phase 4")
        assertTrue(hasPhase, "expected 'Phase 4' in '${err.reason}'")
    }

    @Test fun failureIsStableAcrossSubscribers() = runTest {
        val repo = NoopStatusRepository()
        val a = repo.observeStatus().first()
        val b = repo.observeStatus().first()
        assertEquals((a as Outcome.Failure).error, (b as Outcome.Failure).error)
    }

    @Test fun interfaceIsImplementedByTheNoop() {
        // Compile-time + runtime: the placeholder implements the
        // interface. Phase 4's `PollingStatusRepository` will
        // implement the same interface.
        val repo: StatusRepository = NoopStatusRepository()
        assertEquals(true, repo is NoopStatusRepository)
    }

    @Test fun observeStatusReturnsAFlowOfOutcome() = runTest {
        // Type-shape sanity check. The build itself enforces the
        // type at the declaration site, so this is a no-op that
        // documents the contract.
        val repo = NoopStatusRepository()
        val flow: kotlinx.coroutines.flow.Flow<Outcome<ServerStatus>> = repo.observeStatus()
        val first = flow.first()
        // Use the type explicitly so unused-import warnings stay
        // clean if the file is read in isolation.
        @Suppress("UNUSED_VARIABLE")
        val _doc: Outcome<ServerStatus> = first
    }
}
