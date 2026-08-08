package com.opensapien.relay.ui.commands

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.Spacing
import com.opensapien.relay.data.Command
import com.opensapien.relay.data.CommandStatus
import com.opensapien.relay.ui.design.StatePill
import com.opensapien.relay.ui.design.StateStyle
import com.opensapien.relay.ui.design.Tone
import java.time.Instant

/**
 * One command row. Pure presentation — no state, no IO, no
 * coroutines. All user input is dispatched via [onAck].
 *
 * Layout (top to bottom inside a [Card]):
 *  - title:  command.type
 *  - subtitle: StatusPill + " · " + relativeTime(issuedAt, now)
 *  - reason:  most recent history detail's "reason" for FAILED /
 *             CANCELLED / TIMED_OUT only; absent otherwise
 *  - trailing button (right side): "Ack", shown only when PENDING
 */
@Composable
fun CommandRow(
    command: Command,
    now: Instant,
    onAck: (commandId: String) -> Unit,
    modifier: Modifier = Modifier,
) {
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

    Card(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = 64.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
            contentColor = MaterialTheme.colorScheme.onSurface,
        ),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(Spacing.md),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Spacing.sm),
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = command.type,
                    style = MaterialTheme.typography.titleMedium,
                )
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(Spacing.xs),
                ) {
                    StatePill(
                        style = StateStyle(label = command.status.name, tone = Tone.Neutral),
                    )
                    Text(
                        text = " · ${relativeTime(command.issuedAt, now)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (reason != null) {
                    Text(
                        text = reason,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.testTag("command_row_reason"),
                    )
                }
            }
            if (command.status == CommandStatus.PENDING) {
                Button(
                    onClick = { onAck(command.commandId) },
                    modifier = Modifier
                        .size(width = 72.dp, height = 48.dp)
                        .testTag("command_row_ack"),
                ) {
                    Text("Ack", style = MaterialTheme.typography.labelLarge)
                }
            }
        }
    }
}
