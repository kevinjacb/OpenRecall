package com.opensapien.relay.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.data.ChatMessage
import com.opensapien.relay.data.Role
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.design.SenseScreenHeader

/**
 * Stateless Chat screen. The route builds the ViewModel and collects state;
 * this composable only renders.
 *
 * Layout: the editorial header, then either the empty state or the message
 * list (weight 1f), then the composer pinned to the bottom. The `weight` on
 * the empty state matters — `fillMaxSize` there would consume the column and
 * push the composer off screen.
 *
 * The "is the agent thinking" test (loading, and the last message is the
 * user's) lives here rather than in the route: it is a view concern, not a
 * data one.
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
    speakerError: String? = null,
    onDismissSpeakerError: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    val isThinking = loading && messages.lastOrNull()?.role == Role.USER
    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(speakerError) {
        if (speakerError != null) {
            snackbarHostState.showSnackbar(message = speakerError)
            onDismissSpeakerError()
        }
    }
    Scaffold(
        modifier = modifier,
        containerColor = colors.canvas,
        snackbarHost = { SnackbarHost(hostState = snackbarHostState) },
    ) { padding ->
        Column(
            Modifier
                .padding(padding)
                .fillMaxSize()
                .background(colors.canvas)
                .statusBarsPadding(),
        ) {
            SenseScreenHeader(
                eyebrow = "Chat",
                hero = "Ask what it heard",
                modifier = Modifier.padding(horizontal = 20.dp),
            )
            if (messages.isEmpty()) {
                EmptyState(
                    title = "Nothing asked yet",
                    body = "Try “what did I say about the enclosure yesterday?” — " +
                        "answers cite the memories they used.",
                    modifier = Modifier.weight(1f).testTag("chat_empty"),
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
}
