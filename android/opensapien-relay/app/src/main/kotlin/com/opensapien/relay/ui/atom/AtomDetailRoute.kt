package com.opensapien.relay.ui.atom

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import com.opensapien.relay.data.ChatMessageKind
import com.opensapien.relay.data.RepositoryModule
import com.opensapien.relay.data.Role

/**
 * Route for the atom detail screen. STUB — cheap heuristic lookup
 * in [com.opensapien.relay.data.ChatHistoryStore] for the atom's text.
 * The real AtomDetailScreen will fetch /memory/{atomId} from the
 * server.
 *
 * Lookup logic: find the first AGENT_ANSWER message in the
 * current session's history whose atoms contain one with the
 * given [atomId]; use that atom's text. If no such message exists
 * (the chip was tapped from a prior session that was cleared, or
 * the chat history is empty), pass `text = null` to
 * [AtomDetailScreen] which renders the "not in history" empty
 * state.
 */
@Composable
fun AtomDetailRoute(
    atomId: String,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val store = RepositoryModule.repos.chatHistoryStore
    val messages by store.messages.collectAsState()
    val text = remember(messages, atomId) {
        messages.asSequence()
            .filter { it.role == Role.AGENT && it.kind == ChatMessageKind.AGENT_ANSWER }
            .flatMap { it.atoms.asSequence() }
            .firstOrNull { it.atomId == atomId }
            ?.text
    }
    AtomDetailScreen(atomId = atomId, text = text, modifier = modifier)
    // onBack is accepted for API parity with SessionDetailRoute;
    // the stub does not surface a back button (SenseTopBar with
    // no onBack shows no back arrow, matching the spec).
    @Suppress("UNUSED_EXPRESSION") onBack
}
