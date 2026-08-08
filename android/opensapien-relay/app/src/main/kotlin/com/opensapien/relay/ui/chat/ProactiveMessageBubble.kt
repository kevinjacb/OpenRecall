package com.opensapien.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.data.ChatMessage
import com.opensapien.relay.ui.design.SenseIcons

/**
 * A message the server pushed without being asked. Same left-aligned frame
 * as an agent answer but accent-tinted, with a "Noticed for you" eyebrow —
 * the comp's wording, and clearer than the old "Proactive" label about why
 * an unrequested message appeared.
 *
 * Atom chips render only when the message carries atom *text*: the P3 wire
 * payload sends ids alone, so the row is skipped until a local lookup by
 * atom id lands.
 *
 * INV-11: this file lives under `ui/chat/` and must not import anything from
 * `com.opensapien.relay.http.*` — enforced by `ArchitecturalInvariantsTest`.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun ProactiveMessageBubble(
    message: ChatMessage,
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    ChatBubble(
        author = BubbleAuthor.Proactive,
        modifier = modifier.testTag("chat_proactive_${message.id}"),
    ) { contentColor ->
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            Icon(
                imageVector = SenseIcons.Sparkle,
                contentDescription = "Proactive",
                tint = colors.accentInk,
                modifier = Modifier.size(13.dp).testTag("chat_proactive_icon"),
            )
            Text(
                text = "NOTICED FOR YOU",
                style = MaterialTheme.typography.labelSmall,
                color = colors.accentInk,
                modifier = Modifier.testTag("chat_proactive_tag"),
            )
        }
        Text(
            text = message.text,
            style = MaterialTheme.typography.bodyMedium,
            color = contentColor,
        )
        val displayableAtoms = message.atoms.filter { it.text.isNotEmpty() }
        if (displayableAtoms.isNotEmpty()) {
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
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
