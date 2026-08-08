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
 * Entry point for the Memories tab. Builds a [MemoryViewModel] from
 * [RepositoryModule]'s config-aware memory repository, so a re-provision in
 * Settings takes effect on the next search.
 *
 * [onOpenAtom] deep-links a tapped memory to the atom detail screen.
 */
@Composable
fun MemoryRoute(
    onOpenAtom: (atomId: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val vm: MemoryViewModel = viewModel(
        factory = viewModelFactory {
            initializer { MemoryViewModel(repo = RepositoryModule.repos.memoryRepository) }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    MemoryScreen(
        state = state,
        onQueryChanged = vm::onQueryChanged,
        onSearch = vm::search,
        onFilterSelected = vm::onFilterSelected,
        onAtomTap = onOpenAtom,
        isRefreshing = isRefreshing,
        onRefresh = vm::onRefresh,
        modifier = modifier,
    )
}
