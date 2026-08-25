package com.openrecall.relay

import com.openrecall.relay.protocol.ServerMessage
import com.openrecall.relay.protocol.Wire
import com.openrecall.relay.protocol.parseServerMessage
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive
import java.util.Base64
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
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
    fun device_audio_is_forwarded_verbatim_as_binary_once_started() {
        val session = RelaySession("s")
        session.start()  // hello; discard
        val packet = byteArrayOf(0x11, 0x22, 0x33)
        val action = session.onDeviceAudio(packet).single()
        assertContentEquals(packet, (action as RelayAction.SendServerBinary).data)
    }

    @Test
    fun audio_arriving_before_start_is_held_then_flushed_after_hello() {
        val session = RelaySession("s")
        val p1 = byteArrayOf(0x01)
        val p2 = byteArrayOf(0x02)
        // Audio arrives before the socket is open / hello sent (the GATT
        // thread can race ahead of the WS reader thread's onOpen). It must
        // be held, not forwarded — else the server sees binary before hello
        // and closes 1002 ("audio received before hello").
        assertTrue(session.onDeviceAudio(p1).isEmpty())
        assertTrue(session.onDeviceAudio(p2).isEmpty())

        // start() emits hello FIRST, then the held audio in arrival order.
        val actions = session.start()
        assertEquals("hello", field((actions[0] as RelayAction.SendServerText).text, "type"))
        assertContentEquals(p1, (actions[1] as RelayAction.SendServerBinary).data)
        assertContentEquals(p2, (actions[2] as RelayAction.SendServerBinary).data)

        // After start, audio is forwarded immediately again.
        val p3 = byteArrayOf(0x03)
        assertContentEquals(p3, (session.onDeviceAudio(p3).single() as RelayAction.SendServerBinary).data)
    }

    @Test
    fun stop_drops_held_audio_so_a_reconnect_start_emits_only_hello() {
        val session = RelaySession("s")
        session.onDeviceAudio(byteArrayOf(0x01))  // held, never flushed
        session.stop()  // bye; held audio dropped, started reset

        // A reconnect reuses the same RelaySession: start() must emit only
        // hello, not stale audio from the dropped link.
        val actions = session.start()
        assertEquals(1, actions.size)
        assertEquals("hello", field((actions.single() as RelayAction.SendServerText).text, "type"))
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

    // --- speaker recognition --------------------------------------------------

    @Test
    fun proactive_with_name_speaker_propose_forwards_speaker_nudge_chat_message() {
        val session = RelaySession("s")
        val actions = session.onServerMessage(
            """{"type":"proactive","request_id":"r1","text":"Who was that?",
               "atoms":[],"propose":{"kind":"name_speaker","speaker_id":"sp-9"}}"""
        )
        assertEquals(1, actions.size)
        val forward = actions.single() as RelayAction.ForwardToChatHistory
        val msg = forward.message
        assertEquals(com.openrecall.relay.data.ChatMessageKind.NAME_SPEAKER, msg.kind)
        assertEquals("sp-9", msg.propose?.speakerId)
        assertEquals("s", msg.sessionId)
        assertEquals("sp-9", msg.speakerId)
    }

    @Test
    fun proactive_without_propose_still_forwards_text_as_proactive() {
        val session = RelaySession("s")
        val actions = session.onServerMessage(
            """{"type":"proactive","request_id":"r2","text":"hi","atoms":[]}"""
        )
        val forward = actions.single() as RelayAction.ForwardToChatHistory
        assertEquals(com.openrecall.relay.data.ChatMessageKind.AGENT_PROACTIVE, forward.message.kind)
        assertNull(forward.message.propose)
    }

    @Test
    fun transcript_with_speaker_upserts_speaker_cache() {
        val cache = com.openrecall.relay.data.SpeakerCache()
        val session = RelaySession("s", speakerCache = cache)
        session.onServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000,
               "speaker":"sp-1","speaker_name":"Sarah","is_wearer":false}"""
        )
        assertEquals("Sarah", cache.get("sp-1")?.name)
        assertEquals(false, cache.get("sp-1")?.isWearer)
    }

    @Test
    fun transcript_without_speaker_does_not_touch_cache() {
        val cache = com.openrecall.relay.data.SpeakerCache()
        val session = RelaySession("s", speakerCache = cache)
        session.onServerMessage(
            """{"type":"transcript","session_id":"s","text":"silence","duration_ms":1000}"""
        )
        assertNull(cache.get("nothing"))
        assertEquals(0, cache.snapshot().size)
    }

    // --- A1: ring buffer + request_chunks backfill ---------------------------

    /**
     * Build a minimal §C.6 audio packet with the given [chunkSeq] at bytes 1-4
     * (little-endian uint32) and an empty body. The server's RequestChunks
     * asks for a chunk_seq range the Android relay DID receive and DID send,
     * but the server missed (Android→server WebSocket loss/reorder) — the ring
     * buffer backfills from chunks Android still has. It does NOT backfill
     * BLE-dropped packets (those never reached Android).
     */
    private fun c6Packet(chunkSeq: Int): ByteArray {
        val p = ByteArray(12)  // 12-byte §C.6 header, no frames
        p[0] = 0x10            // version=1, ptype=LIVE=0
        // chunk_seq at bytes 1-4, little-endian
        p[1] = (chunkSeq and 0xFF).toByte()
        p[2] = ((chunkSeq ushr 8) and 0xFF).toByte()
        p[3] = ((chunkSeq ushr 16) and 0xFF).toByte()
        p[4] = ((chunkSeq ushr 24) and 0xFF).toByte()
        // bytes 5-8 rel_ts_ms = 0; byte 9 vad=0; byte 10 frame_count=0; byte 11 flags=0
        return p
    }

    private fun requestChunks(start: Int, end: Int): String =
        """{"type":"request_chunks","session_id":"s","start":$start,"end":$end}"""

    /** Pull the chunk_seq (bytes 1-4 LE) back out of a sent-binary payload. */
    private fun chunkSeqOf(data: ByteArray): Int {
        assertEquals(true, data.size >= 5, "packet too short for chunk_seq")
        return (data[1].toInt() and 0xFF) or
            ((data[2].toInt() and 0xFF) shl 8) or
            ((data[3].toInt() and 0xFF) shl 16) or
            ((data[4].toInt() and 0xFF) shl 24)
    }

    @Test
    fun request_chunks_backfills_in_range_seqs_from_the_ring() {
        val session = RelaySession("s")
        session.start()  // hello; discard
        // Seed the ring with seqs 10, 11, 12, 13, 14.
        for (seq in 10..14) {
            session.onDeviceAudio(c6Packet(seq))
        }
        // Server reports it missed [11, 14) — seqs 11, 12, 13.
        val actions = session.onServerMessage(requestChunks(11, 14))
        val sent = actions.filterIsInstance<RelayAction.SendServerBinary>()
        assertEquals(3, sent.size)
        val seqs = sent.map { chunkSeqOf(it.data) }
        assertEquals(listOf(11, 12, 13), seqs)
    }

    @Test
    fun request_chunks_skips_seqs_not_in_the_ring_without_crashing() {
        val session = RelaySession("s")
        session.start()
        // Ring has seqs 10, 12, 14 (gaps in the ring itself).
        session.onDeviceAudio(c6Packet(10))
        session.onDeviceAudio(c6Packet(12))
        session.onDeviceAudio(c6Packet(14))
        // Server asks for [9, 16) — only 10, 12, 14 are present; 9, 11, 13, 15 skipped.
        val actions = session.onServerMessage(requestChunks(9, 16))
        val sent = actions.filterIsInstance<RelayAction.SendServerBinary>()
        assertEquals(listOf(10, 12, 14), sent.map { chunkSeqOf(it.data) })
    }

    @Test
    fun request_chunks_with_empty_range_returns_no_send_actions() {
        val session = RelaySession("s")
        session.start()
        session.onDeviceAudio(c6Packet(10))
        val actions = session.onServerMessage(requestChunks(10, 10))
        assertTrue(actions.none { it is RelayAction.SendServerBinary })
    }

    @Test
    fun ring_evicts_oldest_past_the_cap_so_old_seqs_are_skipped() {
        val session = RelaySession("s")
        session.start()
        // Push 30 chunks (cap is 25); seqs 0..29. The ring keeps the most recent 25,
        // i.e. seqs 5..29; seqs 0..4 are evicted.
        for (seq in 0 until 30) {
            session.onDeviceAudio(c6Packet(seq))
        }
        // Request an evicted seq (3) — it must be skipped, not crash.
        val actions = session.onServerMessage(requestChunks(0, 5))
        assertTrue(actions.none { it is RelayAction.SendServerBinary },
            "evicted seqs must not be backfilled")
        // Request a retained seq (5) — it must be present.
        val actions2 = session.onServerMessage(requestChunks(5, 6))
        val sent2 = actions2.filterIsInstance<RelayAction.SendServerBinary>()
        assertEquals(listOf(5), sent2.map { chunkSeqOf(it.data) })
    }

    @Test
    fun request_chunks_before_start_does_not_backfill_held_audio() {
        // Before start(), audio is held in pendingAudio (not yet sent) but it
        // IS in the ring. However, the runtime cannot send binary before hello,
        // so a backfill before start() returns no SendServerBinary — the server
        // cannot have requested backfill for a session it hasn't seen hello for.
        // (The ring still holds the chunks for after start().)
        val session = RelaySession("s")
        session.onDeviceAudio(c6Packet(10))
        val actions = session.onServerMessage(requestChunks(10, 11))
        assertTrue(actions.none { it is RelayAction.SendServerBinary })
    }

    @Test
    fun stop_clears_the_ring_so_a_reconnect_request_chunks_backfills_nothing() {
        val session = RelaySession("s")
        session.start()
        session.onDeviceAudio(c6Packet(10))
        session.onDeviceAudio(c6Packet(11))
        session.stop()  // bye; ring cleared
        // Reconnect: re-start and request the old seqs — ring is empty.
        session.start()
        val actions = session.onServerMessage(requestChunks(10, 12))
        assertTrue(actions.none { it is RelayAction.SendServerBinary },
            "stop() must clear the ring so stale chunk_seqs from the dead link are not backfilled")
    }

    @Test
    fun short_or_malformed_packets_are_skipped_by_request_chunks_not_crash() {
        val session = RelaySession("s")
        session.start()
        session.onDeviceAudio(c6Packet(10))
        // A malformed packet (too short for chunk_seq) goes into the ring but
        // is skipped during backfill — it cannot contribute a chunk_seq.
        session.onDeviceAudio(byteArrayOf(0x10, 0x01, 0x02))
        val actions = session.onServerMessage(requestChunks(10, 12))
        val sent = actions.filterIsInstance<RelayAction.SendServerBinary>()
        // Only seq 10 (the well-formed packet) is backfilled.
        assertEquals(listOf(10), sent.map { chunkSeqOf(it.data) })
    }

    // JSON-quote/escape a string as a field value.
    private fun quote(s: String) = JsonPrimitive(s).toString()
}
