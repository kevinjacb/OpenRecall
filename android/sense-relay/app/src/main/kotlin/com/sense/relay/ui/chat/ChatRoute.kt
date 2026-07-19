package com.sense.relay.ui.chat

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.sense.relay.data.RepositoryModule

/**
 * Navigation entry point for the Chat screen. Owns:
 *   - the ChatViewModel (built via the project's manual-DI
 *     viewModelFactory, same pattern as HomeRoute / DeviceRoute /
 *     CommandsRoute).
 *
 * The stateless [ChatScreen] is rendered below. The two
 * onOpen* callbacks are passed in by [com.sense.relay.ui.nav.AppNavigation],
 * which owns the NavController and translates them to
 * `navController.navigate(Destination.AtomDetail.build(id).route)`
 * and `navController.navigate(Destination.Memory.route)`.
 *
 * The route does NOT own a LazyListState — [ChatMessageList] uses
 * its own remembered state. Process death loses scroll position
 * (acceptable for this slice; future polish can persist it via
 * SavedStateHandle).
 */
@Composable
fun ChatRoute(
    onOpenAtom: (atomId: String) -> Unit,
    onOpenMemory: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val vm: ChatViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                ChatViewModel(
                    repo = RepositoryModule.repos.agentRepository,
                    store = RepositoryModule.repos.chatHistoryStore,
                )
            }
        },
    )
    val messages by vm.messages.collectAsState()
    val loading by vm.loading.collectAsState()
    val draft by vm.draft.collectAsState()

    ChatScreen(
        messages = messages,
        loading = loading,
        draft = draft,
        onTextChanged = vm::onTextChanged,
        onSend = vm::ask,
        onAtomChipTap = onOpenAtom,
        onBrowseMemory = onOpenMemory,
        modifier = modifier,
    )
}
