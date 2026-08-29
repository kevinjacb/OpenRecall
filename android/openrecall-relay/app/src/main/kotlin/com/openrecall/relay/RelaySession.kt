package com.openrecall.relay

import com.openrecall.relay.data.AtomChip
import com.openrecall.relay.data.ChatMessage
import com.openrecall.relay.data.ChatMessageKind
import com.openrecall.relay.data.Role
import com.openrecall.relay.protocol.CommandAck
import com.openrecall.relay.protocol.Bye
import com.openrecall.relay.protocol.Hello
import com.openrecall.relay.protocol.ServerMessage
import com.openrecall.relay.protocol.Wire
import com.openrecall.relay.protocol.encode
import com.openrecall.relay.protocol.parseServerMessage
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.ArrayDeque
import java.util.Base64

/**
 * The relay's protocol brain: bridges one BLE device session to one WebSocket server
 * session. Pure and I/O-free — it consumes events and returns [RelayAction]s for the
 * runtime ([RelayService]) to execute. This mirrors the server's `GatewayCore` and is
 * the executable spec for the relay's behaviour (see RelaySessionTest).
 *
 * Tier split: the device speaks only §C.6 audio + §D commands over BLE; THIS layer
 * owns §E (hello/bye/command_ack) and the BLE<->WS translation, including turning a
 * server `command` (base64 sig + JSON payload) into the device's on-wire command
 * frame `[raw 64-byte signature][payload JSON]`.
 */
class RelaySession(
    val sessionId: String,
    private val startSeq: Int = 0,
    private val speakerCache: com.openrecall.relay.data.SpeakerCache? = null,
) {
    companion object {
        /**
         * A1: ring buffer cap — ~5 s of 200 ms audio chunks (5 s / 200 ms = 25).
         * Eviction is oldest-first past this size.
         */
        const val RING_MAX_CHUNKS = 25
    }

    /**
     * True once [start] has emitted `hello` for the current link. The BLE
     * (GATT) thread can deliver [onDeviceAudio] before the WS reader thread
     * fires `onOpen` → [start], and OkHttp transmits any pre-`onOpen` sends in
     * FIFO order — so ungated audio would reach the server *before* `hello`
     * and the server would close 1002 ("audio received before hello"). We hold
     * such audio here and flush it after `hello` so `hello` is always first.
     */
    private var started = false
    private val pendingAudio = ArrayDeque<ByteArray>()

    /**
     * A1: ring buffer of recently-sent §C.6 audio packets, kept so the relay
     * can honor a server [ServerMessage.RequestChunks] backfill. This covers
     * Android→server WebSocket loss or reordering — chunks the relay DID
     * receive from the device and DID send, but the server missed. It does
     * NOT backfill BLE-dropped packets (a BLE-dropped packet never reached
     * Android, so it isn't in the ring). Capped at ~5 s of 200 ms chunks.
     */
    private val sentRing = ArrayDeque<ByteArray>()

    /** Both links are up (device connected, socket open): open the server session. */
    fun start(): List<RelayAction> {
        started = true
        val hello = RelayAction.SendServerText(Hello(sessionId, startSeq).encode())
        val flushed = pendingAudio.map { RelayAction.SendServerBinary(it) }
        pendingAudio.clear()
        return listOf(hello) + flushed
    }

    /**
     * A §C.6 audio packet notified by the device — forward verbatim to the server.
     *
     * Before [start] has run (the socket-open/hello race window), the packet is
     * held in [pendingAudio] and nothing is sent; [start] flushes it after
     * `hello`. After [start], packets are forwarded immediately.
     *
     * A1: every outbound chunk (both branches — held audio is still sent after
     * [start] flushes it, so it belongs in the ring too) is pushed onto
     * [sentRing] so a later [ServerMessage.RequestChunks] can re-send it.
     */
    fun onDeviceAudio(packet: ByteArray): List<RelayAction> =
        if (started) {
            pushRing(packet)
            listOf(RelayAction.SendServerBinary(packet))
        } else {
            pendingAudio.addLast(packet)
            pushRing(packet)
            emptyList()
        }

    /** Push [packet] onto the ring, evicting the oldest past the cap. */
    private fun pushRing(packet: ByteArray) {
        sentRing.addLast(packet)
        while (sentRing.size > RING_MAX_CHUNKS) sentRing.pollFirst()
    }

    /**
     * Extract the §C.6 `chunk_seq` (bytes 1-4, little-endian uint32) from a
     * packet, or null if the packet is too short / malformed. Mirrors the
     * server-side parser (`audio_packet.py`): `_HEADER = "<BIIBBB"`,
     * `chunk_seq` at offset 1. Stored/compared as Kotlin `Int` (signed) — at
     * 200 ms/chunk, 2^31 chunks ≈ 14 years, no wrap concern.
     */
    private fun chunkSeqOf(packet: ByteArray): Int? =
        if (packet.size >= 5)
            ByteBuffer.wrap(packet, 1, 4).order(ByteOrder.LITTLE_ENDIAN).int
        else null

    /** A §E text frame from the server. */
    fun onServerMessage(text: String): List<RelayAction> =
        when (val msg = parseServerMessage(text)) {
            is ServerMessage.Command -> listOf(forwardCommand(msg))
            is ServerMessage.RequestChunks -> backfill(msg.start, msg.end)
            is ServerMessage.Transcript -> {
                // Speaker recognition: upsert the cache so labels and the
                // reassign picker resolve the UUID to the latest name. The
                // store keeps only the UUID; the name is read-time-resolved.
                val spk = msg.speaker
                if (speakerCache != null && spk != null) {
                    speakerCache.upsert(spk, msg.speakerName, msg.isWearer)
                }
                listOf(RelayAction.Note("transcript: ${msg.text}"))
            }
            is ServerMessage.Ack -> emptyList()       // cursor ack; nothing to relay
            is ServerMessage.Proactive -> listOf(forwardProactive(msg))
            is ServerMessage.Unknown -> emptyList()   // forward-compatible: ignore
        }

    /**
     * P3: turn a server-initiated proactive answer into a ChatMessage
     * and hand it to [RelayAction.ForwardToChatHistory] for the runtime
     * to append. The wire payload only carries atom ids; the bubble
     * shows the cited atoms as empty chips (deep-link is the
     * follow-up slice). The request_id becomes the ChatMessage id so
     * later server messages about the same proactive (none today, but
     * the contract is forward-compatible) dedup naturally.
     */
    private fun forwardProactive(msg: ServerMessage.Proactive): RelayAction {
        // Speaker recognition: a name-the-speaker nudge (propose != null) is
        // rendered as an interactive NAME_SPEAKER bubble; otherwise the normal
        // proactive-answer bubble.
        if (msg.propose != null) {
            val chatMessage = ChatMessage(
                id = msg.requestId,
                role = Role.AGENT,
                kind = ChatMessageKind.NAME_SPEAKER,
                text = msg.text,
                propose = msg.propose,
                sessionId = sessionId,
                speakerId = msg.propose.speakerId,
                traceRequestId = msg.requestId,
            )
            return RelayAction.ForwardToChatHistory(chatMessage)
        }
        val chatMessage = ChatMessage(
            id = msg.requestId,
            role = Role.AGENT,
            kind = ChatMessageKind.AGENT_PROACTIVE,
            text = msg.text,
            atoms = msg.atoms.map { atomId ->
                AtomChip(
                    atomId = atomId,
                    sessionId = "",
                    kind = "",
                    text = "",
                    createdAt = "",
                    startMs = 0,
                    score = 0.0,
                )
            },
            traceRequestId = msg.requestId,
        )
        return RelayAction.ForwardToChatHistory(chatMessage)
    }

    /** The device notified a command ack (the command_id bytes) — wrap it as §E. */
    fun onDeviceCommandAck(ackPayload: ByteArray): RelayAction {
        val text = String(ackPayload, Charsets.UTF_8)
        // The ACK characteristic doubles as the device's control uplink: a
        // payload starting with '{' is device control JSON (today: battery
        // telemetry), while a plain command-id ack never starts with '{'.
        // Telemetry gets the session id injected (only the relay knows it)
        // and is forwarded verbatim as the server's §E `telemetry` frame,
        // which feeds /device/status and the app's battery tile.
        if (text.startsWith("{")) {
            val obj = runCatching {
                Wire.json.parseToJsonElement(text) as? JsonObject
            }.getOrNull() ?: return RelayAction.Note("unparseable device control frame")
            val type = (obj["type"] as? JsonPrimitive)?.contentOrNull
            if (type == "telemetry" && started) {
                val withSession = JsonObject(obj + ("session_id" to JsonPrimitive(sessionId)))
                return RelayAction.SendServerText(withSession.toString())
            }
            // Unknown control JSON, or telemetry before hello — the server
            // would close the socket on an unknown session, so drop it; the
            // device re-sends every sample interval.
            return RelayAction.Note("dropped device control frame: $type")
        }
        return RelayAction.SendServerText(CommandAck(sessionId, text).encode())
    }

    /**
     * A1: re-send chunks in the `[start, end)` chunk_seq range from [sentRing].
     *
     * The server's `RequestChunks(start, end)` is triggered when its reassembler
     * detects a `chunk_seq` gap from Android→server WebSocket loss or reordering
     * — chunks the relay DID receive and DID send, but the server missed. The
     * ring buffer backfills those. Seqs not in the ring (evicted, or never
     * received — e.g. BLE-dropped packets never reached Android) are skipped
     * without crashing. Range is `[start, end)` (start inclusive, end
     * exclusive), matching the server's `RequestChunks` semantics.
     *
     * Before [start] has run, returns no backfill — the runtime cannot send
     * binary before `hello` (the server would close 1002), and the server
     * cannot have requested backfill for a session it hasn't seen hello for.
     */
    private fun backfill(start: Int, end: Int): List<RelayAction> {
        if (!started || start >= end) return emptyList()
        val actions = mutableListOf<RelayAction>()
        for (packet in sentRing) {
            val seq = chunkSeqOf(packet) ?: continue  // skip short/malformed
            if (seq in start until end) {
                actions.add(RelayAction.SendServerBinary(packet))
            }
        }
        return actions
    }

    /** Tear down: tell the server the session is closing so it flushes. Resets the
     *  hello/audio gate so a reconnect (same instance, re-[start]) drops any audio
     *  held against the dead link and re-arms the hold for the next link. A1 also
     *  clears [sentRing] — on reconnect the old link's chunk_seqs are stale. */
    fun stop(): List<RelayAction> {
        started = false
        pendingAudio.clear()
        sentRing.clear()
        return listOf(RelayAction.SendServerText(Bye(sessionId).encode()))
    }

    // The device verifies over the raw payload bytes, so the relay reconstructs the
    // on-wire frame: [decoded 64-byte signature][canonical payload JSON bytes].
    private fun forwardCommand(cmd: ServerMessage.Command): RelayAction {
        val signature = Base64.getDecoder().decode(cmd.sig)
        val payload = cmd.payload.toByteArray(Charsets.UTF_8)
        return RelayAction.WriteDeviceCommand(signature + payload)
    }
}

/** Side effects the runtime performs; kept as data so the session stays testable. */
sealed interface RelayAction {
    data class SendServerBinary(val data: ByteArray) : RelayAction
    data class SendServerText(val text: String) : RelayAction
    data class WriteDeviceCommand(val frame: ByteArray) : RelayAction
    /** P3: append the message to the chat history store (process-singleton
     *  on [com.openrecall.relay.data.RepositoryModule.repos]). The runtime's
     *  RelayService handles this branch. */
    data class ForwardToChatHistory(val message: ChatMessage) : RelayAction
    data class Note(val message: String) : RelayAction
}
