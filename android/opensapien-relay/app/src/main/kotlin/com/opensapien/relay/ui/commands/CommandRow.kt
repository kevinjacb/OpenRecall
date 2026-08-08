package com.opensapien.relay.ui.commands

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.data.Command
import com.opensapien.relay.data.CommandStatus
import com.opensapien.relay.ui.design.SecondaryButton
import com.opensapien.relay.ui.design.SenseCard
import com.opensapien.relay.ui.design.StatusBadge
import com.opensapien.relay.ui.design.StatusTone
import java.time.Instant

/**
 * One command card. Pure presentation — no state, no IO, no coroutines. All
 * user input is dispatched via [onAck].
 *
 * Layout, in the comp's card idiom (the same white surface, hairline border
 * and radius the Recordings and Memories rows use):
 *  - a header row: the status pill, and the relative issue time right-aligned
 *  - the command type as the card's own heading
 *  - the failure reason, for FAILED / CANCELLED / TIMED_OUT only
 *  - an "Ack" button, shown only when PENDING
 */
@Composable
fun CommandRow(
    command: Command,
    now: Instant,
    onAck: (commandId: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    val reason = remember(command) {
        if (command.status.isTerminalFailure()) {
            command.history
                .lastOrNull { it.toStatus == command.status }
                ?.detail
                ?.get("reason")
                ?.toString()
                ?.takeIf { it.isNotBlank() }
        } else null
    }

    SenseCard(modifier = modifier) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            StatusBadge(
                label = command.status.displayLabel(),
                tone = command.status.tone(),
            )
            Text(
                text = relativeTime(command.issuedAt, now),
                style = MaterialTheme.typography.bodySmall,
                color = colors.greyFaint,
                modifier = Modifier.weight(1f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Text(
            text = command.type,
            style = MaterialTheme.typography.titleMedium,
            color = colors.ink,
            modifier = Modifier.padding(top = 10.dp),
        )
        if (reason != null) {
            Text(
                text = reason,
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier
                    .padding(top = 6.dp)
                    .testTag("command_row_reason"),
            )
        }
        if (command.status == CommandStatus.PENDING) {
            SecondaryButton(
                label = "Ack",
                onClick = { onAck(command.commandId) },
                modifier = Modifier
                    .padding(top = 14.dp)
                    .testTag("command_row_ack"),
            )
        }
    }
}

/**
 * Wire-format enum → the sentence-case wording the comp uses for every other
 * status. `TIMED_OUT` reads as "Timed out", not as a shouted constant.
 */
internal fun CommandStatus.displayLabel(): String = when (this) {
    CommandStatus.PENDING -> "Pending"
    CommandStatus.VALIDATED -> "Validated"
    CommandStatus.ISSUED -> "Issued"
    CommandStatus.DELIVERED -> "Delivered"
    CommandStatus.EXECUTING -> "Executing"
    CommandStatus.COMPLETED -> "Completed"
    CommandStatus.FAILED -> "Failed"
    CommandStatus.CANCELLED -> "Cancelled"
    CommandStatus.TIMED_OUT -> "Timed out"
}

/**
 * Status → pill tone. Everything mid-lifecycle is [StatusTone.Working] so its
 * dot breathes — a command in flight is the one thing on this screen that is
 * actively changing, and that motion is what tells you polling is alive.
 */
internal fun CommandStatus.tone(): StatusTone = when (this) {
    CommandStatus.PENDING,
    CommandStatus.VALIDATED,
    CommandStatus.ISSUED,
    CommandStatus.DELIVERED,
    CommandStatus.EXECUTING,
    -> StatusTone.Working

    CommandStatus.COMPLETED -> StatusTone.Healthy

    CommandStatus.FAILED,
    CommandStatus.CANCELLED,
    CommandStatus.TIMED_OUT,
    -> StatusTone.Problem
}
