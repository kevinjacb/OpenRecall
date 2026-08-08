package com.openrecall.relay.ui.memory

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
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.ui.design.EmptyState
import com.openrecall.relay.ui.design.FilterPill
import com.openrecall.relay.ui.design.RecallCard
import com.openrecall.relay.ui.design.RecallIcons
import com.openrecall.relay.ui.design.RecallScreenHeader
import com.openrecall.relay.ui.design.RecallSearchField

/**
 * Stateless Memories screen — the comp's "What it decided to keep". An
 * editorial header, a search field, a horizontally scrolling row of kind
 * filters, then one card per memory atom.
 *
 * Search is debounced in the ViewModel. In browse mode the kind filter is a
 * server query, so it reaches memories that were never paged in; in search
 * mode the endpoint takes no kind, so it narrows the hits locally.
 *
 * INV-11: this file lives under `ui/memory/` and must not import anything
 * from `com.openrecall.relay.http.*` — enforced by `ArchitecturalInvariantsTest`.
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
    onLoadMore: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    Column(
        modifier
            .fillMaxSize()
            .background(colors.canvas)
            .statusBarsPadding(),
    ) {
        Column(Modifier.padding(horizontal = 20.dp)) {
            RecallScreenHeader(
                eyebrow = "Memories",
                hero = "What it decided to keep",
                supporting = supportingLine(state),
            )
            RecallSearchField(
                value = state.query,
                onValueChange = onQueryChanged,
                placeholder = "Search memories",
                onSearch = onSearch,
                modifier = Modifier.padding(top = 16.dp).testTag("memory_query"),
            )
            FilterRow(
                filters = state.filters,
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

                else -> AtomList(
                    atoms = state.visibleAtoms,
                    canLoadMore = state.nextCursor != null,
                    onAtomTap = onAtomTap,
                    onLoadMore = onLoadMore,
                )
            }
        }
    }
}

/**
 * The line under the hero — the comp's "128 memories, 6 added today".
 *
 * The totals come from `/memory/stats` and count the whole collection, not
 * the page on screen. "Today" is counted on *conversation* time, not on when
 * the server extracted it: extraction runs in batch, so counting by
 * extraction time would report last night's conversation as added today.
 *
 * While a search is active the totals are the wrong answer to the question
 * the user is asking, so the line reports the hit count instead.
 */
private fun supportingLine(state: MemoryState): String {
    val tail = "Nothing here leaves your relay."
    if (state.lastQuery.isNotEmpty()) {
        val n = state.visibleAtoms.size
        return "${if (n == 1) "1 match" else "$n matches"} for \u201c${state.lastQuery}\u201d. $tail"
    }
    val total = state.stats.total
    if (total == 0 && state.atoms.isEmpty()) {
        return "What your OpenRecall chose to keep. $tail"
    }
    return buildString {
        append(if (total == 1) "1 memory" else "$total memories")
        if (state.stats.added24h > 0) append(", ${state.stats.added24h} added today")
        append(". ")
        append(tail)
    }
}

/**
 * The kind chips. [filters] comes from the server's per-kind counts, so the
 * row shows exactly the kinds that exist — a hardcoded row would offer
 * filters that match nothing while hiding kinds that do.
 */
@Composable
private fun FilterRow(
    filters: List<String>,
    selected: String,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (filters.size <= 1) return
    Row(
        modifier = modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        filters.forEach { filter ->
            FilterPill(
                label = filter.replaceFirstChar { it.uppercase() },
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
            body = "Search across what your OpenRecall has heard. Memories it decides " +
                "are worth keeping show up here.",
            modifier = Modifier.testTag("memory_empty_initial"),
        )
    }
}

@Composable
private fun AtomList(
    atoms: List<MemoryAtom>,
    canLoadMore: Boolean,
    onAtomTap: (String) -> Unit,
    onLoadMore: () -> Unit,
) {
    val listState = rememberLazyListState()
    // Fetch the next page as the last card comes into view. Browse mode is
    // cursor-paged; search mode reports canLoadMore = false and never fires.
    val shouldLoadMore by remember(atoms.size, canLoadMore) {
        derivedStateOf {
            canLoadMore &&
                (listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0) >=
                listState.layoutInfo.totalItemsCount - 2
        }
    }
    LaunchedEffect(shouldLoadMore) { if (shouldLoadMore) onLoadMore() }

    LazyColumn(
        state = listState,
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
    val colors = RecallTheme.colors
    RecallCard(
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
                text = atom.displayedAt,
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
                RecallIcons.Link,
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
