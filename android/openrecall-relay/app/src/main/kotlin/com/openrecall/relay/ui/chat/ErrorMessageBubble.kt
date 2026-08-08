package com.openrecall.relay.ui.chat

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.openrecall.relay.core.ui.RecallTheme

/**
 * Rendered when the agent call fails (network, server error, rate limit).
 * [message] is already user-facing — the route runs the raw error through
 * [com.openrecall.relay.core.ui.toDisplayMessage] first, so this bubble
 * never sees a raw one.
 *
 * The `error_bubble` testTag is the dispatch marker.
 */
@Composable
fun ErrorMessageBubble(
    message: String,
    modifier: Modifier = Modifier,
) {
    ChatBubble(
        author = BubbleAuthor.Agent,
        modifier = modifier.testTag("error_bubble"),
    ) { _ ->
        Text(
            text = message,
            style = MaterialTheme.typography.bodyMedium,
            color = RecallTheme.colors.danger,
        )
    }
}
