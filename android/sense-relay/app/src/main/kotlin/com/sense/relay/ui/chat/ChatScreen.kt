package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.sense.relay.data.ChatMessage
import com.sense.relay.data.Role
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * Stateless Chat screen. The Route builds the ViewModel and
 * collects the state; this Composable just renders.
 *
 * Layout:
 *  - SenseTopBar("Chat")
 *  - if messages.isEmpty(): EmptyState ("Ask the agent", "Try ...")
 *  - else: ChatMessageList (weight=1f)
 *  - ChatInputBar (always at the bottom)
 *
 * The "isThinking" computation (loading && last message is USER)
 * lives in this Composable, not the Route — it's a view-level
 * concern, not a data-layer concern.
 */
@Composable
fun ChatScreen(
    messages: List<ChatMessage>,
    loading: Boolean,
    draft: String,
    onTextChanged: (String) -> Unit,
    onSend: () -> Unit,
    onAtomChipTap: (atomId: String) -> Unit,
    onBrowseMemory: () -> Unit,
    onNameSpeaker: (speakerId: String, name: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val isThinking = loading && messages.lastOrNull()?.role == Role.USER
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Chat"))
        if (messages.isEmpty()) {
            // weight(1f) — takes the remaining vertical space between
            // the top bar and the input bar. fillMaxSize() would consume
            // the whole column and push the ChatInputBar off-screen
            // (the visible bug from the manual smoke test on 2026-07-19).
            EmptyState(
                title = "Ask the agent",
                body = "Try \"what did I say about X yesterday?\" — answers cite " +
                    "the memory atoms they used.",
                modifier = Modifier
                    .weight(1f)
                    .testTag("chat_empty"),
            )
        } else {
            ChatMessageList(
                messages = messages,
                isThinking = isThinking,
                onAtomChipTap = onAtomChipTap,
                onBrowseMemory = onBrowseMemory,
                onNameSpeaker = onNameSpeaker,
                modifier = Modifier.weight(1f),
            )
        }
        ChatInputBar(
            draft = draft,
            onTextChanged = onTextChanged,
            onSend = onSend,
            canSend = draft.trim().isNotEmpty() && !loading,
            isLoading = loading,
            modifier = Modifier.fillMaxWidth(),
        )
    }
}
