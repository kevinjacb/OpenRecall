package com.opensapien.relay.ui.atom

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.design.PlaceholderNote
import com.opensapien.relay.ui.design.SenseCard
import com.opensapien.relay.ui.design.SenseDetailHeader

/**
 * One memory, opened from a chat citation or the Memories list.
 *
 * **Partial placeholder.** The text is real when the atom is in the current
 * chat history; provenance (which session, at what offset, from which
 * extractor) needs a `GET /memory/{atomId}` endpoint that does not exist —
 * only search and per-session listing do. Until then this shows what the
 * client already holds and says so.
 */
@Composable
fun AtomDetailScreen(
    atomId: String,
    text: String?,
    onBack: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    Column(
        modifier
            .fillMaxSize()
            .background(colors.canvas)
            .statusBarsPadding(),
    ) {
        SenseDetailHeader(title = "Memory", onBack = onBack)
        if (text == null) {
            EmptyState(
                title = "Memory not in this session",
                body = "This memory was cited in an earlier conversation that has " +
                    "since been cleared.",
                modifier = Modifier.fillMaxSize().testTag("atom_not_found"),
            )
        } else {
            Column(
                Modifier
                    .padding(horizontal = 20.dp)
                    .testTag("atom_detail"),
            ) {
                SenseCard {
                    Text(
                        text = atomId,
                        style = MaterialTheme.typography.labelSmall,
                        color = colors.accentInk,
                    )
                    Text(
                        text = text,
                        style = MaterialTheme.typography.bodyLarge,
                        color = colors.inkSoft,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
                PlaceholderNote(
                    title = "Provenance",
                    body = "Which session this came from, when it was said, and which " +
                        "extractor produced it — pending a per-memory endpoint on the relay.",
                    modifier = Modifier.padding(top = 12.dp),
                )
            }
        }
    }
}
