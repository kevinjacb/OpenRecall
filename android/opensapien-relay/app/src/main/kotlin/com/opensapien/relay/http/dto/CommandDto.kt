package com.opensapien.relay.http.dto

import com.opensapien.relay.data.Command
import com.opensapien.relay.data.CommandStatus
import com.opensapien.relay.data.CommandStatusTransition
import kotlinx.serialization.Serializable
import java.time.Instant

/**
 * Wire DTOs for /commands endpoints (P2-commands Phase 9).
 *
 * Field names mirror the server's CommandRecordDto and
 * CommandHistoryEntryDto in server/src/opensapien_server/http/routes/commands.py.
 * The Android UI maps these to domain types; the server maps back.
 */
@Serializable
data class CommandHistoryEntryDto(
    val from_status: String? = null,
    val to_status: String,
    val at: String,
    val detail: Map<String, String> = emptyMap(),
)

@Serializable
data class CommandRecordDto(
    val command_id: String,
    val session_id: String,
    val type: String,
    val params: Map<String, String> = emptyMap(),
    val issued_at: String,
    val expires_at: String,
    val status: String,
    val history: List<CommandHistoryEntryDto> = emptyList(),
) {
    fun toDomain(): Command {
        // Detail values are arbitrary JSON; the server serializes them
        // as JSON-encoded strings. We round-trip them as a string-keyed
        // map; the UI treats them as opaque.
        return Command(
            commandId = command_id,
            sessionId = session_id,
            type = type,
            params = params.mapValues { it.value },
            issuedAt = Instant.parse(issued_at),
            expiresAt = Instant.parse(expires_at),
            status = CommandStatus.fromWire(status),
            history = history.map { entry ->
                CommandStatusTransition(
                    fromStatus = entry.from_status?.let { CommandStatus.fromWire(it) },
                    toStatus = CommandStatus.fromWire(entry.to_status),
                    at = Instant.parse(entry.at),
                    detail = entry.detail,
                )
            },
        )
    }
}
