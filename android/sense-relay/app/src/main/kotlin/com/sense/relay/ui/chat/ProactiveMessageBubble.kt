package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.ChatMessage

/**
 * The proactive message bubble. Sibling of [AgentMessageBubble] and
 * [RefuseMessageBubble]: same bubble styling, with a small "Proactive"
 * tag at the top so the user knows the server pushed this without
 * being asked. Atom chips are rendered when the message carries
 * non-empty atom text (today the wire payload only carries atom ids,
 * so the chip row is skipped; the deep-link follow-up slice will
 * populate the chips from the local memory cache).
 *
 * INV-11: this Composable lives in `ui/chat/`. It must not import
 * any class under `com.sense.relay.http.*` or
 * `com.sense.relay.http.dto.*`. The architectural invariant test
 * (`ArchitecturalInvariantsTest`) enforces this.
 */
@Composable
fun ProactiveMessageBubble(
    message: ChatMessage,
    modifier: Modifier = Modifier,
) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = Spacing.xs)
            .testTag("chat_proactive_${message.id}"),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    imageVector = Icons.Filled.Notifications,
                    contentDescription = "Proactive",
                    modifier = Modifier.testTag("chat_proactive_icon"),
                )
                Spacer(Modifier.width(Spacing.xs))
                Text(
                    text = "Proactive",
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.testTag("chat_proactive_tag"),
                )
            }
            Spacer(Modifier.height(Spacing.xs))
            Text(
                text = message.text,
                style = MaterialTheme.typography.bodyLarge,
            )
            // Only render chips when the server gave us text to display.
            // The P3 wire payload carries only atom ids; the chip row
            // becomes useful once the deep-link follow-up slice wires
            // a local memory cache lookup by atom id.
            val displayableAtoms = message.atoms.filter { it.text.isNotEmpty() }
            if (displayableAtoms.isNotEmpty()) {
                Spacer(Modifier.height(Spacing.xs))
                Row(horizontalArrangement = Arrangement.spacedBy(Spacing.xs)) {
                    displayableAtoms.forEach { chip ->
                        AtomChip(
                            text = chip.text,
                            onClick = { /* deep-link is the follow-up slice */ },
                        )
                    }
                }
            }
        }
    }
}
