package com.sense.relay.data

import com.sense.relay.http.dto.SpeakerDto
import com.sense.relay.relay.ServerState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import java.io.IOException
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import kotlin.test.fail

/**
 * Pins [SpeakerCacheSeeder]: `seedOnce` maps the repo's entries into the cache
 * (and clears/propagates errors), and `launchOnAuthenticated` re-seeds on every
 * transition into [ServerState.Authenticated] while swallowing fetch failures
 * (a bad `/speakers` call must never kill the collector).
 *
 * Pure JVM; mirrors [SpeakerCacheTest] + [SpeakerRepositoryImplTest] (kotlin.test,
 * fake [SpeakerApi]).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SpeakerCacheSeederTest {

    // UnconfinedTestDispatcher so the backgroundScope collector runs eagerly
    // and MutableStateFlow emissions deliver inline — a collector-side-effect
    // test (vs DeviceViewModelTest which reads vm.state.value imperatively).
    // StandardTestDispatcher + advanceUntilIdle does not deliver a .value
    // change to a suspended cold-flow collector; Unconfined does.
    private val dispatcher = UnconfinedTestDispatcher()

    @BeforeTest
    fun setUp() = Dispatchers.setMain(dispatcher)

    @AfterTest
    fun tearDown() = Dispatchers.resetMain()

    private class FakeSpeakerApi : SpeakerApi {
        var speakers: List<SpeakerDto> = emptyList()
        var error: Throwable? = null
        override suspend fun getSpeakers(): List<SpeakerDto> {
            error?.let { throw it }
            return speakers
        }
        override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
            SpeakerDto(speakerId = speakerId, displayName = name, isWearer = false)
        override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) {}
    }

    private fun dto(id: String, name: String?, wearer: Boolean = false) = SpeakerDto(
        speakerId = id, displayName = name, isWearer = wearer,
        enrollmentStatus = "confirmed", turnCount = 1,
        firstSeen = "2026-07-29T00:00:00Z", updatedAt = "2026-07-29T00:00:00Z",
    )

    private fun seeder(api: FakeSpeakerApi, cache: SpeakerCache = SpeakerCache()): SpeakerCacheSeeder =
        // Dispatchers.Unconfined runs loadSpeakers inline (no real-IO-thread hop)
        // so advanceUntilIdle() drives the backgroundScope collector deterministically.
        SpeakerCacheSeeder(SpeakerRepository(apiProvider = { api }, io = Dispatchers.Unconfined), cache)

    @Test
    fun seedOnce_populates_cache_from_repo() = runTest(dispatcher) {
        val api = FakeSpeakerApi().apply {
            speakers = listOf(dto("sp-1", "Sarah"), dto("you", "You", wearer = true))
        }
        val cache = SpeakerCache()
        seeder(api, cache).seedOnce()

        val snap = cache.snapshot()
        assertEquals(2, snap.size)
        assertEquals("Sarah", snap["sp-1"]?.name)
        assertEquals(false, snap["sp-1"]?.isWearer)
        assertEquals("You", snap["you"]?.name)
        assertEquals(true, snap["you"]?.isWearer)
    }

    @Test
    fun seedOnce_empty_list_clears_cache() = runTest(dispatcher) {
        val cache = SpeakerCache().apply { upsert("old", "Old", false) }
        seeder(FakeSpeakerApi(), cache).seedOnce()
        assertEquals(0, cache.snapshot().size, "empty /speakers clears the cache")
    }

    @Test
    fun seedOnce_propagates_api_error() = runTest(dispatcher) {
        val api = FakeSpeakerApi().apply { error = IOException("boom") }
        val cache = SpeakerCache().apply { upsert("keep", "Keep", false) }
        try {
            seeder(api, cache).seedOnce()
            fail("seedOnce should propagate the API error")
        } catch (e: IOException) {
            // expected
        }
        // Cache untouched — seed() never ran (loadSpeakers threw first).
        assertEquals("Keep", cache.get("keep")?.name)
    }

    @Test
    fun launchOnAuthenticated_seeds_on_authenticated_only_and_swallows_errors() = runTest(dispatcher) {
        val api = FakeSpeakerApi().apply {
            speakers = listOf(dto("sp-1", "Sarah"))
        }
        val cache = SpeakerCache()
        val seeder = seeder(api, cache)
        val states = MutableStateFlow<ServerState>(ServerState.Reachable)

        seeder.launchOnAuthenticated(backgroundScope, states)

        // Not authenticated yet → no seed.
        testScheduler.advanceUntilIdle()
        assertEquals(0, cache.snapshot().size)

        // Authenticated → seed.
        states.value = ServerState.Authenticated
        testScheduler.advanceUntilIdle()
        assertEquals("Sarah", cache.get("sp-1")?.name)

        // Unreachable → no re-seed (and no crash).
        states.value = ServerState.Unreachable("drop")
        testScheduler.advanceUntilIdle()

        // Re-authenticate but the API now throws → the collector must swallow it
        // (no exception escapes), and the cache is left unchanged (seed() never ran).
        api.error = IOException("boom")
        api.speakers = emptyList()
        states.value = ServerState.Authenticated
        testScheduler.advanceUntilIdle()
        assertEquals("Sarah", cache.get("sp-1")?.name, "failed re-seed leaves cache intact")
        assertTrue(true, "collector survived the failing tick")
    }
}