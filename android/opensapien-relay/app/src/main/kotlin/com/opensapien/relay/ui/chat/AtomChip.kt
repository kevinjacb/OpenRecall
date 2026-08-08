package com.opensapien.relay.ui.chat

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.opensapien.relay.ui.design.AccentChip

/**
 * A tappable pill for one cited memory atom — the design system's
 * [AccentChip]. The caller truncates the text; this composable does not.
 *
 * The `atom_chip` testTag is the dispatch marker.
 */
@Composable
fun AtomChip(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    AccentChip(
        label = text,
        onClick = onClick,
        modifier = modifier.testTag("atom_chip"),
    )
}
