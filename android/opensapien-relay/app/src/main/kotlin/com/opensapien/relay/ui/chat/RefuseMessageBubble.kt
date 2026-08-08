package com.opensapien.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.Spacing

/**
 * Rendered when the agent returns a Refuse outcome (no supporting
 * memory). Shows the friendly copy + a "browse memory directly"
 * text button that fires [onBrowseMemory] — the route wires this
 * to `navController.navigate(Destination.Memory.route)`.
 *
 * The `refuse_bubble` testTag is the dispatch marker for
 * ChatMessageList; `refuse_browse_memory` is the inner button.
 */
@Composable
fun RefuseMessageBubble(
    onBrowseMemory: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.Start,
    ) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = MaterialTheme.colorScheme.surfaceVariant,
            contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier
                .widthIn(max = 320.dp)
                .testTag("refuse_bubble"),
        ) {
            Column(modifier = Modifier.padding(Spacing.md)) {
                Text(
                    text = "I don't have a memory about that yet. Try asking after " +
                        "the wearable has captured more, or",
                    style = MaterialTheme.typography.bodyLarge,
                )
                TextButton(
                    onClick = onBrowseMemory,
                    modifier = Modifier.testTag("refuse_browse_memory"),
                ) {
                    Text("browse memory directly")
                }
            }
        }
    }
}
