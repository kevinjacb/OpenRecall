package com.openrecall.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.openrecall.relay.data.AtomChip as DomainAtomChip

/**
 * An agent answer. Left-aligned white card; when the answer cites memory
 * atoms they appear as a wrapping row of tappable chips beneath the text.
 *
 * Chip text is truncated to 80 characters — the chip is a label, not the
 * citation; the full text lives on the server and behind the tap.
 *
 * The `agent_bubble` testTag is the dispatch marker for [ChatMessageList].
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun AgentMessageBubble(
    text: String,
    atoms: List<DomainAtomChip>,
    onAtomChipTap: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    ChatBubble(
        author = BubbleAuthor.Agent,
        modifier = modifier.testTag("agent_bubble"),
    ) { contentColor ->
        Text(
            text = text,
            style = MaterialTheme.typography.bodyMedium,
            color = contentColor,
        )
        if (atoms.isNotEmpty()) {
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                atoms.forEach { atom ->
                    AtomChip(
                        text = atom.text.truncateForChip(),
                        onClick = { onAtomChipTap(atom.atomId) },
                    )
                }
            }
        }
    }
}

/** A defensive UI cap so one long citation can't dominate the chip row. */
private fun String.truncateForChip(): String =
    if (length > 80) take(79) + "…" else this
