package com.sense.relay.protocol

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * §E control-plane envelope, mirroring the server's `sense_server.protocol.messages`.
 *
 * The relay OWNS §E: it speaks hello/bye/command_ack up to the server, and interprets
 * the server's ack/request_chunks/transcript/command coming down. Audio itself rides
 * as binary §C.6 frames (see [com.sense.relay.RelaySession]); these are only control.
 *
 * Inbound is parsed leniently by discriminating on `type` so an unknown/newer message
 * type is ignored rather than crashing the relay.
 */
object Wire {
    val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
}

// ---- Outbound (relay -> server) ----

@Serializable
data class Hello(
    val session_id: String,
    val start_seq: Int = 0,
    val type: String = "hello",
)

@Serializable
data class Bye(
    val session_id: String,
    val type: String = "bye",
)

@Serializable
data class CommandAck(
    val session_id: String,
    val command_id: String,
    val type: String = "command_ack",
)

fun Hello.encode(): String = Wire.json.encodeToString(Hello.serializer(), this)
fun Bye.encode(): String = Wire.json.encodeToString(Bye.serializer(), this)
fun CommandAck.encode(): String = Wire.json.encodeToString(CommandAck.serializer(), this)

// ---- Inbound (server -> relay), decoded to a small sealed model ----

sealed interface ServerMessage {
    /** Cursor ack: the next contiguous chunk_seq the server wants. */
    data class Ack(val nextSeq: Int) : ServerMessage

    /** Backfill request for the contiguous gap [start, end). */
    data class RequestChunks(val start: Int, val end: Int) : ServerMessage

    /** A transcribed window (informational to the relay). */
    data class Transcript(val text: String, val durationMs: Int) : ServerMessage

    /** A signed §D command to forward to the device. `payload` is canonical JSON,
     *  `sig` is base64 — the device needs raw signature bytes prepended to payload. */
    data class Command(val payload: String, val sig: String) : ServerMessage

    /** Any type the relay doesn't handle. */
    data class Unknown(val type: String) : ServerMessage
}

/** Parse one server §E text frame. Never throws on unknown/missing fields. */
fun parseServerMessage(text: String): ServerMessage {
    val obj = runCatching { Wire.json.parseToJsonElement(text) as? JsonObject }.getOrNull()
        ?: return ServerMessage.Unknown("malformed")
    fun str(k: String) = (obj[k]?.jsonPrimitive?.content)
    fun int(k: String) = (obj[k]?.jsonPrimitive?.content?.toIntOrNull())
    return when (str("type")) {
        "ack" -> ServerMessage.Ack(int("next_seq") ?: 0)
        "request_chunks" -> ServerMessage.RequestChunks(int("start") ?: 0, int("end") ?: 0)
        "transcript" -> ServerMessage.Transcript(str("text") ?: "", int("duration_ms") ?: 0)
        "command" -> ServerMessage.Command(str("payload") ?: "", str("sig") ?: "")
        else -> ServerMessage.Unknown(str("type") ?: "missing")
    }
}
