package com.opensapien.relay.data

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class SpeakerLabelsTest {

    @Test
    fun unnamed_non_wearer_speakers_get_person_N_in_first_appearance_order() {
        val speakers = listOf(
            SpeakerEntry("you", "You", isWearer = true),
            SpeakerEntry("sp-2", null, isWearer = false),  // appears first among unnamed
            SpeakerEntry("sp-1", "Sarah", isWearer = false), // already named -> no label
            SpeakerEntry("sp-3", null, isWearer = false),  // appears second among unnamed
        )
        val labels = personLabels(speakers)
        assertEquals("Person 1", labels["sp-2"])
        assertEquals("Person 2", labels["sp-3"])
        // Named speakers and the wearer are NOT labelled (the caller renders
        // their real name / "You").
        assertNull(labels["sp-1"])
        assertNull(labels["you"])
    }

    @Test
    fun empty_when_everyone_is_named_or_wearer() {
        val speakers = listOf(
            SpeakerEntry("you", "You", isWearer = true),
            SpeakerEntry("sp-1", "Sarah", isWearer = false),
        )
        assertEquals(emptyMap(), personLabels(speakers))
    }
}
