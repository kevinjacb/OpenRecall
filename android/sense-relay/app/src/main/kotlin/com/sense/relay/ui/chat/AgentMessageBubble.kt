package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.AtomChip as DomainAtomChip

/**
 * An agent answer bubble. Left-aligned, surfaceVariant background.
 * If [atoms] is non-empty, a row of [AtomChip]s is rendered below
 * the text — tappable, navigates to AtomDetail on tap (the
 * onAtomChipTap callback is fired with the atom's id).
 *
 * Atom chip text is truncated to 80 chars at the call site (here)
 * with a trailing "…" if longer. The full text is on the server;
 * the chip is a label, not the citation.
 *
 * The `agent_bubble` testTag is the dispatch marker for
 * ChatMessageList.
 */
@Composable
fun AgentMessageBubble(
    text: String,
    atoms: List<DomainAtomChip>,
    onAtomChipTap: (String) -> Unit,
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
                .testTag("agent_bubble"),
        ) {
            Column(modifier = Modifier.padding(Spacing.md)) {
                Text(text = text, style = MaterialTheme.typography.bodyLarge)
                if (atoms.isNotEmpty()) {
                    Spacer(Modifier.height(Spacing.xs))
                    Row(horizontalArrangement = Arrangement.spacedBy(Spacing.xs)) {
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
    }
}

/**
 * Truncate atom text for chip display. The full text is on the
 * server; the chip is a label. 80 chars is a defensive UI cap so
 * a single chip doesn't dominate the row.
 */
private fun String.truncateForChip(): String =
    if (length > 80) take(79) + "…" else this
