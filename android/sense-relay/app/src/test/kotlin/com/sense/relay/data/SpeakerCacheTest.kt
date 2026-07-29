package com.sense.relay.data

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SpeakerCacheTest {

    @Test
    fun seed_replaces_snapshot() {
        val cache = SpeakerCache()
        cache.seed(listOf(
            SpeakerEntry("sp-1", "Sarah", false),
            SpeakerEntry("you", "You", true),
        ))
        assertEquals("Sarah", cache.get("sp-1")?.name)
        assertEquals(true, cache.get("you")?.isWearer)
        assertEquals(2, cache.snapshot().size)
    }

    @Test
    fun upsert_adds_and_overwrites_a_single_entry() {
        val cache = SpeakerCache()
        cache.upsert("sp-1", "Sara", false)
        assertEquals("Sara", cache.get("sp-1")?.name)
        cache.upsert("sp-1", "Sarah", false)
        assertEquals("Sarah", cache.get("sp-1")?.name)
    }

    @Test
    fun get_returns_null_for_unknown() {
        val cache = SpeakerCache()
        assertNull(cache.get("nope"))
    }

    @Test
    fun seed_with_empty_clears_known_speakers() {
        val cache = SpeakerCache()
        cache.upsert("sp-1", "Sarah", false)
        cache.seed(emptyList())
        assertTrue(cache.snapshot().isEmpty())
    }
}