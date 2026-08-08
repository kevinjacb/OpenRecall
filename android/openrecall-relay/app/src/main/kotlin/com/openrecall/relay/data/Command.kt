package com.openrecall.relay.data

import java.time.Instant

/**
 * Lifecycle state of a server-issued command (P2-commands Phase 9).
 *
 * Mirrors the wire-format strings used by the server's
 * ``commands.status`` module. The Android UI renders these as
 * lifecycle badges; the persistence layer serializes them as the
 * ``status`` column on a command record.
 */
enum class CommandStatus {
    PENDING,
    VALIDATED,
    ISSUED,
    DELIVERED,
    EXECUTING,
    COMPLETED,
    FAILED,
    CANCELLED,
    TIMED_OUT;

    val wire: String get() = name

    companion object {
        /** Parse a wire string into a CommandStatus. Throws on unknown. */
        fun fromWire(s: String): CommandStatus = valueOf(s)
    }
}

/**
 * One lifecycle transition (from → to) for a command.
 *
 * The Android UI uses the history to render a vertical timeline
 * (PENDING → ISSUED at 12:00:30, etc.). Mirrors the server's
 * ``StatusTransition`` dataclass.
 */
data class CommandStatusTransition(
    val fromStatus: CommandStatus?,
    val toStatus: CommandStatus,
    val at: Instant,
    val detail: Map<String, Any?> = emptyMap(),
)

/**
 * A command issued by the server (mirrors server's
 * ``commands.model.Command`` + ``commands.record.CommandRecord``).
 */
data class Command(
    val commandId: String,
    val sessionId: String,
    val type: String,
    val params: Map<String, Any?>,
    val issuedAt: Instant,
    val expiresAt: Instant,
    val status: CommandStatus,
    val history: List<CommandStatusTransition> = emptyList(),
)
