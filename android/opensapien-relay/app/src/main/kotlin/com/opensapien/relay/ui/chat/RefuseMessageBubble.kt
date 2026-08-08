package com.opensapien.relay.ui.chat

import androidx.compose.foundation.clickable
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.opensapien.relay.core.ui.SenseTheme

/**
 * Rendered when the agent refuses for want of a supporting memory. The
 * "browse memories" link fires [onBrowseMemory], which the route wires to
 * the Memories tab.
 *
 * Test tags: `refuse_bubble` is the dispatch marker; `refuse_browse_memory`
 * is the link.
 */
@Composable
fun RefuseMessageBubble(
    onBrowseMemory: () -> Unit,
    modifier: Modifier = Modifier,
) {
    ChatBubble(
        author = BubbleAuthor.Agent,
        modifier = modifier.testTag("refuse_bubble"),
    ) { contentColor ->
        Text(
            text = "I don't have a memory about that yet. Try again once your OpenSapien " +
                "has heard more, or",
            style = MaterialTheme.typography.bodyMedium,
            color = contentColor,
        )
        Text(
            text = "browse memories",
            style = MaterialTheme.typography.labelMedium,
            color = SenseTheme.colors.accent,
            modifier = Modifier
                .clickable(onClick = onBrowseMemory)
                .testTag("refuse_browse_memory"),
        )
    }
}
