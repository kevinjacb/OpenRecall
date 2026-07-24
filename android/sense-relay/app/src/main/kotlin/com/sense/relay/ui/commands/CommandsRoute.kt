package com.sense.relay.ui.commands

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.sense.relay.data.CommandsViewModel
import com.sense.relay.data.RepositoryModule
import kotlinx.coroutines.launch

/**
 * Navigation entry point for the Commands screen. Owns:
 *   - the ViewModel (built via the project's manual-DI `viewModelFactory`)
 *   - the lifecycle-aware polling (start on ON_RESUME, stop on ON_PAUSE)
 *
 * The stateless [CommandsScreen] is rendered below.
 */
@Composable
fun CommandsRoute(modifier: Modifier = Modifier) {
    val scope = rememberCoroutineScope()
    val vm: CommandsViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                CommandsViewModel(repo = RepositoryModule.repos.commandRepository)
            }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()

    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { vm.startPolling() }
    LifecycleEventEffect(Lifecycle.Event.ON_PAUSE) { vm.stopPolling() }

    CommandsScreen(
        state = state,
        onAck = { id -> scope.launch { vm.ack(id) } },
        onRetry = { scope.launch { vm.refresh() } },
        isRefreshing = isRefreshing,
        onRefresh = vm::onRefresh,
        modifier = modifier,
    )
}
