package com.opensapien.relay.ui.memory

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.data.MemoryAtom
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.design.FilterPill
import com.opensapien.relay.ui.design.SenseCard
import com.opensapien.relay.ui.design.SenseIcons
import com.opensapien.relay.ui.design.SenseScreenHeader
import com.opensapien.relay.ui.design.SenseSearchField

/**
 * Stateless Memories screen — the comp's "What it decided to keep". An
 * editorial header, a search field, a horizontally scrolling row of kind
 * filters, then one card per memory atom.
 *
 * Search is debounced in the ViewModel (150ms). Filters narrow the results
 * already returned; they are not sent to the server (see
 * [MemoryViewModel.onFilterSelected]).
 *
 * INV-11: this file lives under `ui/memory/` and must not import anything
 * from `com.opensapien.relay.http.*` — enforced by `ArchitecturalInvariantsTest`.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MemoryScreen(
    state: MemoryState,
    onQueryChanged: (String) -> Unit,
    onSearch: () -> Unit,
    onFilterSelected: (String) -> Unit,
    onAtomTap: (String) -> Unit,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    Column(
        modifier
            .fillMaxSize()
            .background(colors.canvas)
            .statusBarsPadding(),
    ) {
        Column(Modifier.padding(horizontal = 20.dp)) {
            SenseScreenHeader(
                eyebrow = "Memories",
                hero = "What it decided to keep",
                supporting = supportingLine(state),
            )
            SenseSearchField(
                value = state.query,
                onValueChange = onQueryChanged,
                placeholder = "Search memories",
                onSearch = onSearch,
                modifier = Modifier.padding(top = 16.dp).testTag("memory_query"),
            )
            FilterRow(
                selected = state.filter,
                onSelect = onFilterSelected,
                modifier = Modifier.padding(top = 16.dp),
            )
        }

        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = onRefresh,
            modifier = Modifier.fillMaxSize(),
        ) {
            when {
                state.loading -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    CircularProgressIndicator(
                        color = colors.accent,
                        modifier = Modifier.testTag("memory_loading"),
                    )
                }

                state.errorMessage != null -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    EmptyState(
                        title = "Couldn't load memories",
                        body = state.errorMessage,
                        ctaLabel = "Try again",
                        onCta = onRefresh,
                        modifier = Modifier.testTag("memory_error"),
                    )
                }

                state.visibleAtoms.isEmpty() -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    EmptyMemories(state)
                }

                else -> AtomList(atoms = state.visibleAtoms, onAtomTap = onAtomTap)
            }
        }
    }
}

/**
 * The line under the hero. The comp reads "128 memories, 6 added today" —
 * the server exposes no memory totals or per-day counts (only search and
 * per-session lookups), so this reports what is actually on screen.
 */
private fun supportingLine(state: MemoryState): String = when {
    state.lastQuery.isEmpty() && state.atoms.isEmpty() ->
        "Search what your OpenSapien chose to remember. Nothing here leaves your relay."
    state.filter != MemoryState.ALL_FILTER ->
        "${state.visibleAtoms.size} of ${state.atoms.size} memories. Nothing here leaves your relay."
    else ->
        "${state.atoms.size} memories. Nothing here leaves your relay."
}

@Composable
private fun FilterRow(
    selected: String,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        MemoryState.FILTERS.forEach { filter ->
            FilterPill(
                label = filter,
                selected = filter == selected,
                onClick = { onSelect(filter) },
            )
        }
    }
}

@Composable
private fun EmptyMemories(state: MemoryState) {
    when {
        state.atoms.isNotEmpty() -> EmptyState(
            title = "No ${state.filter.lowercase()}",
            body = "None of these memories are tagged “${state.filter}”.",
            modifier = Modifier.testTag("memory_empty_no_match"),
        )
        state.lastQuery.isNotEmpty() -> EmptyState(
            title = "No matches",
            body = "No memory matches “${state.lastQuery}”.",
            modifier = Modifier.testTag("memory_empty_no_match"),
        )
        else -> EmptyState(
            title = "Nothing kept yet",
            body = "Search across what your OpenSapien has heard. Memories it decides " +
                "are worth keeping show up here.",
            modifier = Modifier.testTag("memory_empty_initial"),
        )
    }
}

@Composable
private fun AtomList(atoms: List<MemoryAtom>, onAtomTap: (String) -> Unit) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().testTag("memory_list"),
        verticalArrangement = Arrangement.spacedBy(10.dp),
        contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 16.dp, bottom = 28.dp),
    ) {
        items(atoms, key = { it.atomId }) { atom ->
            MemoryCard(atom = atom, onTap = { onAtomTap(atom.atomId) })
        }
    }
}

@Composable
private fun MemoryCard(atom: MemoryAtom, onTap: () -> Unit) {
    val colors = SenseTheme.colors
    SenseCard(
        onClick = onTap,
        modifier = Modifier.testTag("memory_atom_${atom.atomId}"),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = atom.kind.uppercase(),
                style = MaterialTheme.typography.labelSmall,
                color = colors.accentInk,
            )
            Text(
                text = atom.createdAt,
                style = MaterialTheme.typography.labelMedium,
                color = colors.greyFaint,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Text(
            text = atom.text,
            style = MaterialTheme.typography.bodyLarge,
            color = colors.inkSoft,
            modifier = Modifier.padding(top = 8.dp),
        )
        Row(
            modifier = Modifier.padding(top = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Icon(
                SenseIcons.Link,
                contentDescription = null,
                tint = colors.grey,
                modifier = Modifier.size(13.dp),
            )
            Text(
                text = "Session ${atom.sessionId.take(8)} · +${atom.startMs / 1000}s",
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
            )
        }
    }
}
