package com.openrecall.relay

import com.openrecall.relay.protocol.NameSpeakerPropose
import com.openrecall.relay.protocol.ServerMessage
import com.openrecall.relay.protocol.parsePropose
import com.openrecall.relay.protocol.parseServerMessage
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class MessagesParseTest {

    @Test
    fun transcript_carries_speaker_uuid_name_and_is_wearer() {
        val msg = parseServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000,
               "speaker":"sp-1","speaker_name":"Sarah","is_wearer":false}"""
        )
        assertTrue(msg is ServerMessage.Transcript)
        assertEquals("sp-1", msg.speaker)
        assertEquals("Sarah", msg.speakerName)
        assertEquals(false, msg.isWearer)
    }

    @Test
    fun transcript_speaker_fields_default_when_absent() {
        val msg = parseServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000}"""
        )
        assertTrue(msg is ServerMessage.Transcript)
        assertNull(msg.speaker)
        assertNull(msg.speakerName)
        assertEquals(false, msg.isWearer)
    }

    @Test
    fun transcript_speaker_fields_null_when_present_json_null() {
        // The server emits a present JSON null ("speaker":null) for a hop with
        // no resolved speaker. The parser must surface Kotlin null, not the
        // literal string "null" (JsonNull.content == "null" is the bug).
        val msg = parseServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000,
               "speaker":null,"speaker_name":null,"is_wearer":null}"""
        )
        assertTrue(msg is ServerMessage.Transcript)
        assertNull(msg.speaker)
        assertNull(msg.speakerName)
        assertEquals(false, msg.isWearer)
    }

    @Test
    fun proactive_with_name_speaker_propose_is_parsed() {
        val msg = parseServerMessage(
            """{"type":"proactive","request_id":"r1","text":"Who was that?",
               "atoms":[],"propose":{"kind":"name_speaker","speaker_id":"sp-9"}}"""
        )
        assertTrue(msg is ServerMessage.Proactive)
        val propose = msg.propose
        assertTrue(propose != null)
        assertEquals("sp-9", propose.speakerId)
    }

    @Test
    fun proactive_with_unknown_propose_kind_yields_null() {
        val msg = parseServerMessage(
            """{"type":"proactive","request_id":"r1","text":"x","atoms":[],
               "propose":{"kind":"future_thing","speaker_id":"sp-9"}}"""
        )
        assertTrue(msg is ServerMessage.Proactive)
        assertNull(msg.propose)
    }

    @Test
    fun proactive_without_propose_yields_null() {
        val msg = parseServerMessage(
            """{"type":"proactive","request_id":"r1","text":"x","atoms":[]}"""
        )
        assertTrue(msg is ServerMessage.Proactive)
        assertNull(msg.propose)
    }

    @Test
    fun parse_propose_helper_returns_null_for_missing_speaker_id() {
        val obj = JsonObject(mapOf("kind" to JsonPrimitive("name_speaker")))
        assertNull(parsePropose(obj))
    }

    @Test
    fun name_speaker_propose_data_class_holds_speaker_id() {
        val p = NameSpeakerPropose("sp-7")
        assertEquals("sp-7", p.speakerId)
    }
}