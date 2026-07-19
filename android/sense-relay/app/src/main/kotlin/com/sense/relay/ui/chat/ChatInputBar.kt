package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.core.ui.TouchTarget

/**
 * The send-text-and-send bar at the bottom of the chat screen.
 *
 * - The send button is `enabled = canSend` (= draft is non-blank
 *   and we're not currently loading). The user explicitly chose
 *   "send button only" in brainstorming; Enter-to-send is not wired
 *   (ImeAction.Default).
 * - When [isLoading] is true, the send button shows a 20dp
 *   CircularProgressIndicator in place of the send icon. The
 *   button is non-interactive during loading (canSend is false).
 * - The OutlinedTextField is `enabled = !isLoading` so the user
 *   can't type a second draft while the agent is working. Multiple
 *   in-flight ask() calls are not supported; the VM flow is
 *   "submit, wait, get one reply."
 */
@Composable
fun ChatInputBar(
    draft: String,
    onTextChanged: (String) -> Unit,
    onSend: () -> Unit,
    canSend: Boolean,
    isLoading: Boolean,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(Spacing.md),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Spacing.sm),
    ) {
        OutlinedTextField(
            value = draft,
            onValueChange = onTextChanged,
            placeholder = { Text("Ask the agent...") },
            modifier = Modifier
                .weight(1f)
                .testTag("chat_input"),
            enabled = !isLoading,
            maxLines = 4,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Default),
        )
        IconButton(
            onClick = onSend,
            enabled = canSend,
            modifier = Modifier
                .size(TouchTarget)
                .testTag("chat_send"),
        ) {
            if (isLoading) {
                CircularProgressIndicator(
                    modifier = Modifier.size(20.dp),
                    strokeWidth = 2.dp,
                    color = MaterialTheme.colorScheme.primary,
                )
            } else {
                Icon(
                    imageVector = Icons.AutoMirrored.Filled.Send,
                    contentDescription = "Send",
                )
            }
        }
    }
}
