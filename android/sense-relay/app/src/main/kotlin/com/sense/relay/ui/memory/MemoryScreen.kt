package com.sense.relay.ui.memory

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.MemoryAtom
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * Stateless Memory screen. The Route owns the ViewModel; this
 * Composable only renders. Search is debounced inside the ViewModel
 * (150ms); pressing the search button calls [onSearch] which
 * triggers the debounce.
 *
 * Atoms are listed in the order returned by the server (the scorer
 * is responsible for ranking). Each row shows the atom's kind,
 * truncated text, and a relative-timestamp placeholder. Tapping a
 * row fires [onAtomTap] for the future AtomDetail deep-link (no-op
 * for this slice).
 *
 * INV-11: this Composable lives in `ui/memory/`. It must not import
 * any class under `com.sense.relay.http.*` or
 * `com.sense.relay.http.dto.*`. The architectural invariant test
 * (`ArchitecturalInvariantsTest`) enforces this.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MemoryScreen(
    state: MemoryState,
    onQueryChanged: (String) -> Unit,
    onSearch: () -> Unit,
    onAtomTap: (String) -> Unit,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Memory"))
        SearchBar(
            query = state.query,
            onQueryChanged = onQueryChanged,
            onSearch = onSearch,
        )
        // Pull-to-refresh wraps the result region: a pull-down re-runs the
        // last action (the last search, or the deep-link session load).
        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = onRefresh,
            modifier = Modifier.fillMaxSize(),
        ) {
            when {
                state.loading -> LoadingState()
                state.errorMessage != null -> ErrorState(message = state.errorMessage)
                state.atoms.isEmpty() -> EmptyMemoryState(query = state.lastQuery)
                else -> AtomList(atoms = state.atoms, onAtomTap = onAtomTap)
            }
        }
    }
}

@Composable
private fun SearchBar(
    query: String,
    onQueryChanged: (String) -> Unit,
    onSearch: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = Spacing.md, vertical = Spacing.sm),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        OutlinedTextField(
            value = query,
            onValueChange = onQueryChanged,
            placeholder = { Text("Search memory…") },
            singleLine = true,
            modifier = Modifier
                .weight(1f)
                .testTag("memory_query"),
        )
        IconButton(
            onClick = onSearch,
            modifier = Modifier.testTag("memory_search"),
        ) {
            Icon(Icons.Filled.Search, contentDescription = "Search")
        }
    }
}

@Composable
private fun LoadingState() {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(Spacing.md),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        CircularProgressIndicator(modifier = Modifier.testTag("memory_loading"))
    }
}

@Composable
private fun ErrorState(message: String) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(Spacing.md),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.error,
            modifier = Modifier.testTag("memory_error"),
        )
    }
}

@Composable
private fun EmptyMemoryState(query: String) {
    if (query.isNotEmpty()) {
        EmptyState(
            title = "No matches",
            body = "No memory atoms match \"$query\".",
            modifier = Modifier
                .fillMaxSize()
                .testTag("memory_empty_no_match"),
        )
    } else {
        EmptyState(
            title = "Memory",
            body = "Search across your captured transcripts. " +
                "Type a query above and tap the search icon.",
            modifier = Modifier
                .fillMaxSize()
                .testTag("memory_empty_initial"),
        )
    }
}

@Composable
private fun AtomList(
    atoms: List<MemoryAtom>,
    onAtomTap: (String) -> Unit,
) {
    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .testTag("memory_list"),
        verticalArrangement = Arrangement.spacedBy(Spacing.sm),
        contentPadding = PaddingValues(Spacing.md),
    ) {
        items(atoms, key = { it.atomId }) { atom ->
            AtomRow(atom = atom, onTap = { onAtomTap(atom.atomId) })
        }
    }
}

@Composable
private fun AtomRow(
    atom: MemoryAtom,
    onTap: () -> Unit,
) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier
            .fillMaxWidth()
            .testTag("memory_atom_${atom.atomId}")
            .clickable { onTap() },
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Text(
                text = atom.kind,
                style = MaterialTheme.typography.labelSmall,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                text = atom.text,
                style = MaterialTheme.typography.bodyLarge,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(Spacing.xs))
            Text(
                text = atom.createdAt,
                style = MaterialTheme.typography.labelSmall,
            )
        }
    }
}
