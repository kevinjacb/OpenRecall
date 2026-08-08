package com.opensapien.relay.ui.memory

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.opensapien.relay.data.RepositoryModule

/**
 * Real entry point for the Memory screen. Builds a [MemoryViewModel]
 * from [RepositoryModule.repos.memoryRepository] (a config-aware
 * factory, so a re-provision in Settings takes effect on the next
 * search) and renders the stateless [MemoryScreen].
 *
 * The deep-link entry from ChatScreen's refuse-link navigates here
 * with no arguments. A future per-session filter (e.g.
 * `Destination.Memory.withSessionId(id)`) can read it from
 * `backStackEntry.arguments` without changing the route.
 *
 * The `onAtomTap` handler is a no-op for this slice: the full
 * AtomDetailScreen Composable is a follow-up. Tapping an atom in
 * the list currently does nothing.
 */
@Composable
fun MemoryRoute(modifier: Modifier = Modifier) {
    val vm: MemoryViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                MemoryViewModel(
                    repo = RepositoryModule.repos.memoryRepository,
                )
            }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    MemoryScreen(
        state = state,
        onQueryChanged = vm::onQueryChanged,
        onSearch = vm::search,
        onAtomTap = { /* TODO: navigate to AtomDetail in a follow-up slice */ },
        isRefreshing = isRefreshing,
        onRefresh = vm::onRefresh,
        modifier = modifier,
    )
}
