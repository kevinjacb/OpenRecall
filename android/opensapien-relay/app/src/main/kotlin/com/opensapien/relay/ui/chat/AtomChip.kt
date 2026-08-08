package com.opensapien.relay.ui.chat

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow

/**
 * A small outlined pill for one cited atom. Wraps Material 3's
 * [SuggestionChip] (the closest built-in to "tappable label"). The
 * `agent_bubble` callsite truncates the text to 80 chars before
 * passing it in; this Composable does not truncate.
 *
 * The `atom_chip` testTag is the dispatch marker.
 */
@Composable
fun AtomChip(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    SuggestionChip(
        onClick = onClick,
        label = {
            Text(
                text = text,
                style = MaterialTheme.typography.labelSmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        },
        modifier = modifier.testTag("atom_chip"),
    )
}
