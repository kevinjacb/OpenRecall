package com.sense.relay

import com.sense.relay.protocol.ServerMessage
import com.sense.relay.protocol.Wire
import com.sense.relay.protocol.parseServerMessage
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive
import java.util.Base64
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Executable spec for the relay's protocol brain. Pure JVM — no BLE, no socket.
 * These pin the BLE<->WS translation that the Android I/O layers depend on.
 */
class RelaySessionTest {

    private fun field(text: String, key: String): String? =
        ((Wire.json.parseToJsonElement(text) as JsonObject)[key])?.jsonPrimitive?.content

    @Test
    fun start_opens_the_session_with_hello() {
        val actions = RelaySession("sess-1", startSeq = 3).start()

        val text = (actions.single() as RelayAction.SendServerText).text
        assertEquals("hello", field(text, "type"))
        assertEquals("sess-1", field(text, "session_id"))
        assertEquals("3", field(text, "start_seq"))
    }

    @Test
    fun device_audio_is_forwarded_verbatim_as_binary() {
        val packet = byteArrayOf(0x11, 0x22, 0x33)
        val action = RelaySession("s").onDeviceAudio(packet)
        assertContentEquals(packet, (action as RelayAction.SendServerBinary).data)
    }

    @Test
    fun server_command_becomes_a_device_write_of_rawsig_plus_payload() {
        val sig = ByteArray(64) { it.toByte() }
        val sigB64 = Base64.getEncoder().encodeToString(sig)
        val payload = """{"command_id":"c1","type":"capture_photo"}"""
        val msg = """{"type":"command","session_id":"s","payload":${quote(payload)},"sig":"$sigB64"}"""

        val actions = RelaySession("s").onServerMessage(msg)

        val frame = (actions.single() as RelayAction.WriteDeviceCommand).frame
        assertContentEquals(sig, frame.copyOfRange(0, 64))                       // raw signature first
        assertContentEquals(payload.toByteArray(), frame.copyOfRange(64, frame.size))  // then payload JSON
    }

    @Test
    fun device_command_ack_is_wrapped_as_e_command_ack() {
        val action = RelaySession("sess-1").onDeviceCommandAck("c1".toByteArray())

        val text = (action as RelayAction.SendServerText).text
        assertEquals("command_ack", field(text, "type"))
        assertEquals("sess-1", field(text, "session_id"))
        assertEquals("c1", field(text, "command_id"))
    }

    @Test
    fun ack_is_silently_consumed_and_transcript_is_noted() {
        val session = RelaySession("s")
        assertTrue(session.onServerMessage("""{"type":"ack","session_id":"s","next_seq":5}""").isEmpty())

        val note = session.onServerMessage(
            """{"type":"transcript","session_id":"s","text":"hello","duration_ms":5000}"""
        ).single()
        assertTrue((note as RelayAction.Note).message.contains("hello"))
    }

    @Test
    fun stop_sends_bye() {
        val text = (RelaySession("s").stop().single() as RelayAction.SendServerText).text
        assertEquals("bye", field(text, "type"))
    }

    @Test
    fun malformed_and_unknown_server_frames_do_not_crash() {
        assertTrue(parseServerMessage("not json") is ServerMessage.Unknown)
        assertTrue(parseServerMessage("""{"type":"future_thing"}""") is ServerMessage.Unknown)
        assertTrue(RelaySession("s").onServerMessage("garbage").isEmpty())
    }

    // JSON-quote/escape a string as a field value.
    private fun quote(s: String) = JsonPrimitive(s).toString()
}
