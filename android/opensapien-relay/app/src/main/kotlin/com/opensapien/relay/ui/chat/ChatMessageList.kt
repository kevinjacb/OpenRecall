package com.opensapien.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.Spacing
import com.opensapien.relay.data.ChatMessage
import com.opensapien.relay.data.ChatMessageKind

/**
 * The chat message list. Renders one bubble per [ChatMessage],
 * dispatching by [ChatMessageKind] with an exhaustive `when` (a
 * future variant is a compile error in this file). When
 * [isThinking] is true, a [ThinkingBubble] is appended below the
 * last message — the "agent is working" affordance.
 *
 * Auto-scrolls to the last item on every [messages] change and
 * when [isThinking] flips false -> true. The [listState] is
 * remembered by default; the Route can pass its own (so the scroll
 * position survives process death if the Route persists the state
 * — not in this slice).
 *
 * **Test tag:** `chat_list` (matches the spec's manual-validation
 * checklist; in this slice, no host-JVM Compose UI tests, so the
 * testTag is for manual / future tests).
 */
@Composable
fun ChatMessageList(
    messages: List<ChatMessage>,
    isThinking: Boolean,
    onAtomChipTap: (String) -> Unit,
    onBrowseMemory: () -> Unit,
    onNameSpeaker: (speakerId: String, name: String) -> Unit,
    modifier: Modifier = Modifier,
    listState: LazyListState = rememberLazyListState(),
) {
    LaunchedEffect(messages.size, isThinking) {
        if (messages.isNotEmpty()) {
            listState.animateScrollToItem(messages.lastIndex)
        }
    }
    LazyColumn(
        state = listState,
        modifier = modifier.fillMaxSize().testTag("chat_list"),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = Spacing.md),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        items(messages, key = { it.id }) { msg ->
            when (msg.kind) {
                ChatMessageKind.USER_TEXT -> UserMessageBubble(text = msg.text)
                ChatMessageKind.AGENT_ANSWER -> AgentMessageBubble(
                    text = msg.text,
                    atoms = msg.atoms,
                    onAtomChipTap = onAtomChipTap,
                )
                ChatMessageKind.AGENT_REFUSE -> RefuseMessageBubble(
                    onBrowseMemory = onBrowseMemory,
                )
                ChatMessageKind.AGENT_ERROR -> ErrorMessageBubble(
                    message = msg.text.removePrefix("Error: "),
                )
                // P3: server-initiated proactive answer. Same
                // surface as an AGENT_ANSWER bubble, plus a "Proactive"
                // tag so the user knows they didn't ask.
                ChatMessageKind.AGENT_PROACTIVE -> ProactiveMessageBubble(message = msg)
                // Speaker recognition: server asks the user to name an
                // unknown speaker. Interactive bubble with a text field.
                ChatMessageKind.NAME_SPEAKER -> NameSpeakerBubble(
                    message = msg,
                    onNameSpeaker = onNameSpeaker,
                )
            }
        }
        if (isThinking) {
            // `key = "thinking"` keeps the bubble stable across
            // recompositions and prevents Compose from re-creating
            // the rememberInfiniteTransition each frame.
            item(key = "thinking") { ThinkingBubble() }
        }
    }
}
