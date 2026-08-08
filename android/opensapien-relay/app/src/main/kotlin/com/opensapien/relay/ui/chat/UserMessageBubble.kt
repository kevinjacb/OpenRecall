package com.opensapien.relay.ui.chat

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag

/**
 * A user-typed message: ink fill, right-aligned, notched on the sender's
 * side. The `user_bubble` testTag is the dispatch marker for
 * [ChatMessageList].
 */
@Composable
fun UserMessageBubble(
    text: String,
    modifier: Modifier = Modifier,
) {
    ChatBubble(
        author = BubbleAuthor.You,
        modifier = modifier.testTag("user_bubble"),
    ) { contentColor ->
        Text(
            text = text,
            style = MaterialTheme.typography.bodyMedium,
            color = contentColor,
        )
    }
}
