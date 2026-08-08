package com.opensapien.relay.ui.atom

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.opensapien.relay.core.ui.Spacing
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.design.SenseTopBar
import com.opensapien.relay.ui.design.TopBarState

/**
 * Atom detail screen. STUB for this slice — the real
 * AtomDetailScreen (full text + provenance + jump-to-session) is
 * its own slice. Reachable from ChatScreen's atom-chip tap.
 *
 * If [text] is null (the atom isn't in the current session
 * history), shows an EmptyState with the atom id. Otherwise shows
 * the atom id + the (truncated) chip text + a "coming soon" line.
 */
@Composable
fun AtomDetailScreen(
    atomId: String,
    text: String?,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Atom"))
        if (text == null) {
            EmptyState(
                title = "Atom $atomId",
                body = "This atom isn't in the current session history.",
                modifier = Modifier
                    .fillMaxSize()
                    .testTag("atom_not_found"),
            )
        } else {
            Column(
                modifier = Modifier
                    .padding(Spacing.md)
                    .testTag("atom_detail"),
            ) {
                Text(
                    text = atomId,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(Spacing.sm))
                Text(
                    text = text,
                    style = MaterialTheme.typography.bodyLarge,
                )
                Spacer(Modifier.height(Spacing.md))
                Text(
                    text = "Full detail (provenance, jump-to-session) coming soon.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}
