package com.opensapien.relay.data

import com.opensapien.relay.http.dto.SpeakerDto
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class SpeakerRepositoryImplTest {

    private class FakeSpeakerApi : SpeakerApi {
        var speakers: List<SpeakerDto> = emptyList()
        var error: Throwable? = null
        override suspend fun getSpeakers(): List<SpeakerDto> {
            error?.let { throw it }
            return speakers
        }
        override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
            SpeakerDto(speakerId = speakerId, displayName = name, isWearer = true)
        override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) {}
    }

    private fun dto(id: String, name: String?, wearer: Boolean = false) = SpeakerDto(
        speakerId = id, displayName = name, isWearer = wearer,
        enrollmentStatus = "confirmed", turnCount = 1,
        firstSeen = "2026-07-29T00:00:00Z", updatedAt = "2026-07-29T00:00:00Z",
    )

    @Test
    fun loadSpeakers_maps_dtos_to_entries() = runTest {
        val api = FakeSpeakerApi().apply {
            speakers = listOf(dto("sp-1", "Sarah"), dto("you", "You", wearer = true))
        }
        val repo = SpeakerRepository(api)
        val entries = repo.loadSpeakers()
        assertEquals(2, entries.size)
        assertEquals("Sarah", entries.first { it.speakerId == "sp-1" }.name)
        assertEquals(true, entries.first { it.speakerId == "you" }.isWearer)
    }

    @Test
    fun loadSpeakers_propagates_errors() = runTest {
        val api = FakeSpeakerApi().apply { error = java.io.IOException("boom") }
        val repo = SpeakerRepository(api)
        var threw = false
        try { repo.loadSpeakers() } catch (e: Exception) { threw = true }
        assertTrue(threw)
    }

    @Test
    fun loadSpeakers_empty_when_no_speakers() = runTest {
        val repo = SpeakerRepository(FakeSpeakerApi())
        assertEquals(0, repo.loadSpeakers().size)
    }

    @Test
    fun renameSpeakerDelegatesToApi() = runTest {
        val api = object : SpeakerApi {
            var renamed: Pair<String, String>? = null
            override suspend fun getSpeakers(): List<SpeakerDto> = emptyList()
            override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
                SpeakerDto(speakerId = speakerId, displayName = name, isWearer = true).also { renamed = speakerId to name }
            override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) {}
        }
        val repo = SpeakerRepository(api)
        val entry = repo.renameSpeaker("sp-1", "Sarah")
        assertEquals("Sarah", entry.name)
        assertEquals(true, entry.isWearer)
        assertEquals("sp-1" to "Sarah", api.renamed)
    }

    @Test
    fun reassignSpeakerDelegatesToApiWithScopeAll() = runTest {
        val api = object : SpeakerApi {
            var reassigned: Triple<String, String, String>? = null
            override suspend fun getSpeakers(): List<SpeakerDto> = emptyList()
            override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
                SpeakerDto(speakerId = speakerId)
            override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) {
                reassigned = Triple(fromId, toId, scope)
            }
        }
        val repo = SpeakerRepository(api)
        repo.reassignSpeaker("a", "b")
        assertEquals(Triple("a", "b", "all"), api.reassigned)
    }
}