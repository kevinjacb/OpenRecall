package com.openrecall.relay.protocol

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * §E control-plane envelope, mirroring the server's `openrecall_server.protocol.messages`.
 *
 * The relay OWNS §E: it speaks hello/bye/command_ack up to the server, and interprets
 * the server's ack/request_chunks/transcript/command coming down. Audio itself rides
 * as binary §C.6 frames (see [com.openrecall.relay.RelaySession]); these are only control.
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

/** Parsed from a proactive `propose` payload. Only `name_speaker` is recognized
 *  today; unknown kinds yield null (forward-compat, matches the lenient parser). */
data class NameSpeakerPropose(val speakerId: String)

// ---- Inbound (server -> relay), decoded to a small sealed model ----

sealed interface ServerMessage {
    /** Cursor ack: the next contiguous chunk_seq the server wants. */
    data class Ack(val nextSeq: Int) : ServerMessage

    /** Backfill request for the contiguous gap [start, end). */
    data class RequestChunks(val start: Int, val end: Int) : ServerMessage

    /** A transcribed window (informational to the relay). */
    data class Transcript(
        val text: String,
        val durationMs: Int,
        val speaker: String? = null,
        val speakerName: String? = null,
        val isWearer: Boolean = false,
    ) : ServerMessage

    /** A signed §D command to forward to the device. `payload` is canonical JSON,
     *  `sig` is base64 — the device needs raw signature bytes prepended to payload. */
    data class Command(val payload: String, val sig: String) : ServerMessage

    /** P3: a server-initiated proactive answer. The relay forwards it
     *  into the phone's ChatHistoryStore; the chat screen renders it
     *  as an AGENT_PROACTIVE ChatMessage. When [propose] is non-null the
     *  nudge is interactive (name-the-speaker), rendered as NAME_SPEAKER. */
    data class Proactive(
        val requestId: String,
        val text: String,
        val atoms: List<String>,
        val propose: NameSpeakerPropose? = null,
    ) : ServerMessage

    /** Any type the relay doesn't handle. */
    data class Unknown(val type: String) : ServerMessage
}

/** Parse one server §E text frame. Never throws on unknown/missing fields. */
fun parseServerMessage(text: String): ServerMessage {
    val obj = runCatching { Wire.json.parseToJsonElement(text) as? JsonObject }.getOrNull()
        ?: return ServerMessage.Unknown("malformed")
    fun str(k: String): String? {
        val e = obj[k] ?: return null
        if (e is JsonNull) return null
        return e.jsonPrimitive.content
    }
    fun int(k: String) = (obj[k]?.jsonPrimitive?.content?.toIntOrNull())
    fun bool(k: String): Boolean = obj[k]?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: false
    return when (str("type")) {
        "ack" -> ServerMessage.Ack(int("next_seq") ?: 0)
        "request_chunks" -> ServerMessage.RequestChunks(int("start") ?: 0, int("end") ?: 0)
        "transcript" -> ServerMessage.Transcript(
            text = str("text") ?: "",
            durationMs = int("duration_ms") ?: 0,
            speaker = str("speaker"),
            speakerName = str("speaker_name"),
            isWearer = bool("is_wearer"),
        )
        "command" -> ServerMessage.Command(str("payload") ?: "", str("sig") ?: "")
        "proactive" -> ServerMessage.Proactive(
            requestId = str("request_id") ?: "",
            text = str("text") ?: "",
            atoms = (obj["atoms"] as? kotlinx.serialization.json.JsonArray)
                ?.mapNotNull { runCatching { it.jsonPrimitive.content }.getOrNull() }
                ?: emptyList(),
            propose = parsePropose(obj["propose"] as? JsonObject),
        )
        else -> ServerMessage.Unknown(str("type") ?: "missing")
    }
}

/** Only `name_speaker` is recognized today; unknown kinds -> null (forward-compat). */
fun parsePropose(propose: JsonObject?): NameSpeakerPropose? {
    if (propose == null) return null
    val kind = propose["kind"]?.jsonPrimitive?.content
    if (kind != "name_speaker") return null
    val speakerId = propose["speaker_id"]?.jsonPrimitive?.content ?: return null
    return NameSpeakerPropose(speakerId = speakerId)
}
