package com.openrecall.relay.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.ui.design.RecallIcons
import com.openrecall.relay.ui.design.RecallTextField

/**
 * The chat composer: a bordered field with a filled circular send button,
 * pinned above the keyboard.
 *
 * Send is a button press only — Enter inserts a newline, matching the
 * product decision recorded when the bar was first built.
 *
 * While [isLoading] the field is disabled and the button shows a spinner:
 * the flow is submit → wait → one reply, with no support for concurrent
 * in-flight asks.
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
    val colors = RecallTheme.colors
    Column(
        modifier
            .fillMaxWidth()
            .background(colors.canvas)
            .imePadding()
            .navigationBarsPadding(),
    ) {
        Box(Modifier.fillMaxWidth().height(1.dp).background(colors.divider))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            RecallTextField(
                value = draft,
                onValueChange = onTextChanged,
                placeholder = "Ask about anything it heard",
                enabled = !isLoading,
                singleLine = false,
                modifier = Modifier.weight(1f).testTag("chat_input"),
            )
            IconButton(
                onClick = onSend,
                enabled = canSend,
                modifier = Modifier
                    .size(46.dp)
                    .clip(CircleShape)
                    .background(if (canSend) colors.ink else colors.track)
                    .testTag("chat_send"),
            ) {
                if (isLoading) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(18.dp),
                        strokeWidth = 2.dp,
                        color = colors.grey,
                    )
                } else {
                    Icon(
                        imageVector = RecallIcons.Send,
                        contentDescription = "Send",
                        tint = if (canSend) colors.card else colors.greyLight,
                        modifier = Modifier.size(18.dp),
                    )
                }
            }
        }
    }
}
