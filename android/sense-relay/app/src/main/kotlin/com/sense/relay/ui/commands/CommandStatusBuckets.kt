package com.sense.relay.ui.commands

import com.sense.relay.data.CommandStatus

/**
 * Bucketing helpers used by [CommandsScreen] to split the
 * `CommandsViewModel.UiState.Ready.commands` list into
 * "in flight" (active) and "most recent failure" (terminal).
 *
 * Both helpers are exhaustive over the sealed [CommandStatus]
 * enum: adding a new variant is a compile error here, which is
 * the right failure mode — the spec for that future phase must
 * update this file.
 */
fun CommandStatus.isInFlight(): Boolean = when (this) {
    CommandStatus.PENDING,
    CommandStatus.VALIDATED,
    CommandStatus.ISSUED,
    CommandStatus.DELIVERED,
    CommandStatus.EXECUTING,
    -> true

    CommandStatus.COMPLETED,
    CommandStatus.FAILED,
    CommandStatus.CANCELLED,
    CommandStatus.TIMED_OUT,
    -> false
}

fun CommandStatus.isTerminalFailure(): Boolean = when (this) {
    CommandStatus.FAILED,
    CommandStatus.CANCELLED,
    CommandStatus.TIMED_OUT,
    -> true

    CommandStatus.PENDING,
    CommandStatus.VALIDATED,
    CommandStatus.ISSUED,
    CommandStatus.DELIVERED,
    CommandStatus.EXECUTING,
    CommandStatus.COMPLETED,
    -> false
}
