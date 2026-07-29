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
import androidx.compose.material.icons.filled.Person
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.ChatMessage

/**
 * Interactive name-the-speaker nudge. Renders the proactive text plus an
 * inline text field + "Name" button. On submit it calls [onNameSpeaker]
 * with `(speakerId, name)`; the caller (ChatViewModel) sends the
 * `name_speaker` control message via [com.sense.relay.data.SpeakerActions]
 * and optimistically updates the bubble. Correctness arrives on the next
 * transcript §E carrying the new `speaker_name`.
 *
 * Architectural invariant: this composable MUST NOT import
 * `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`
 * (enforced by ArchitecturalInvariantsTest).
 */
@Composable
fun NameSpeakerBubble(
    message: ChatMessage,
    onNameSpeaker: (speakerId: String, name: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var draft by remember { mutableStateOf("") }
    val speakerId = message.propose?.speakerId ?: message.speakerId ?: ""

    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = Spacing.xs)
            .testTag("chat_name_speaker_${message.id}"),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    imageVector = Icons.Filled.Person,
                    contentDescription = "Name speaker",
                    modifier = Modifier.testTag("chat_name_speaker_icon"),
                )
                Spacer(Modifier.width(Spacing.xs))
                Text(
                    text = "Name speaker",
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.testTag("chat_name_speaker_tag"),
                )
            }
            Spacer(Modifier.height(Spacing.xs))
            Text(
                text = message.text,
                style = MaterialTheme.typography.bodyLarge,
            )
            Spacer(Modifier.height(Spacing.sm))
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(Spacing.xs),
            ) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    singleLine = true,
                    placeholder = { Text("Enter a name") },
                    modifier = Modifier
                        .weight(1f)
                        .testTag("chat_name_speaker_field"),
                )
                Button(
                    onClick = {
                        val name = draft.trim()
                        if (name.isNotEmpty() && speakerId.isNotEmpty()) {
                            onNameSpeaker(speakerId, name)
                        }
                    },
                    enabled = draft.isNotBlank() && speakerId.isNotEmpty(),
                    modifier = Modifier.testTag("chat_name_speaker_submit"),
                ) { Text("Name") }
            }
        }
    }
}