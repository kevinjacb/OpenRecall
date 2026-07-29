package com.sense.relay.data

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class SpeakerControlPortTest {

    @Test
    fun nameSpeaker_enqueues_a_name_speaker_frame() {
        SpeakerControlPort.nameSpeaker("s1", "sp-9", "Sarah")
        assertTrue(SpeakerControlPort.hasPending())
        val json = SpeakerControlPort.poll()
        assertFalse(SpeakerControlPort.hasPending())
        assertNotNull(json)
        assertTrue(json!!.contains("\"type\":\"name_speaker\""))
        assertTrue(json.contains("\"speaker_id\":\"sp-9\""))
        assertTrue(json.contains("\"name\":\"Sarah\""))
        assertTrue(json.contains("\"session_id\":\"s1\""))
    }

    @Test
    fun reassignSpeaker_enqueues_a_reassign_frame_with_scope_all() {
        SpeakerControlPort.reassignSpeaker("s1", "sp-1", "sp-2")
        val json = SpeakerControlPort.poll()!!
        assertTrue(json.contains("\"type\":\"reassign_speaker\""))
        assertTrue(json.contains("\"from_speaker_id\":\"sp-1\""))
        assertTrue(json.contains("\"to_speaker_id\":\"sp-2\""))
        assertTrue(json.contains("\"scope\":\"all\""))
    }

    @Test
    fun poll_returns_null_when_empty() {
        // Drain any leftover from prior tests (singleton carries state).
        while (SpeakerControlPort.poll() != null) Unit
        assertEquals(null, SpeakerControlPort.poll())
    }
}