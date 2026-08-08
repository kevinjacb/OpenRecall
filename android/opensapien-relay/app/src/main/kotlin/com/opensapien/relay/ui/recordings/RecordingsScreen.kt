package com.opensapien.relay.ui.recordings

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.core.ui.toDisplayMessage
import com.opensapien.relay.core.util.formatHmMs
import com.opensapien.relay.core.util.formatRelative
import com.opensapien.relay.data.RepositoryModule
import com.opensapien.relay.domain.model.SessionId
import com.opensapien.relay.domain.model.SessionSummary
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.design.SecondaryButton
import com.opensapien.relay.ui.design.SenseCard
import com.opensapien.relay.ui.design.SenseGroupLabel
import com.opensapien.relay.ui.design.SenseScreenHeader
import com.opensapien.relay.ui.design.SenseSearchField

/**
 * Recordings route. Builds the [RecordingsViewModel] from the process
 * singleton and forwards row taps to [onOpen] (nav to session detail).
 */
@Composable
fun RecordingsRoute(onOpen: (SessionId) -> Unit, modifier: Modifier = Modifier) {
    val vm: RecordingsViewModel = viewModel(
        factory = viewModelFactory {
            initializer { RecordingsViewModel(RepositoryModule.repos.session) }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    val query by vm.query.collectAsState()
    RecordingsScreen(
        state = state,
        query = query,
        onQueryChange = vm::onQueryChange,
        isRefreshing = isRefreshing,
        onOpen = onOpen,
        onLoadMore = vm::onLoadMore,
        onRefresh = vm::onRefresh,
        modifier = modifier,
    )
}

/**
 * Stateless Recordings content — the comp's "Everything it heard": an
 * editorial header, a search field, then sessions grouped by recency
 * (Today / Yesterday / Earlier).
 *
 * The list is a single [LazyColumn] carrying the header and search field as
 * items, so the whole screen scrolls as one surface. Reaching the last row
 * triggers [onLoadMore] (cursor pagination); pull-to-refresh resets to
 * page 1.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RecordingsScreen(
    state: RecordingsUiState,
    query: String = "",
    onQueryChange: (String) -> Unit = {},
    isRefreshing: Boolean = false,
    onOpen: (SessionId) -> Unit,
    onLoadMore: () -> Unit,
    onRefresh: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    PullToRefreshBox(
        isRefreshing = isRefreshing,
        onRefresh = onRefresh,
        modifier = modifier.fillMaxSize().background(colors.canvas),
    ) {
        when (state) {
            is RecordingsUiState.Loading -> Centered {
                CircularProgressIndicator(color = colors.accent)
            }

            is RecordingsUiState.Empty -> HeaderThen(query, onQueryChange) {
                EmptyState(
                    title = if (query.isBlank()) "No recordings yet" else "Nothing matches",
                    body = if (query.isBlank()) {
                        "Sessions from your Sense will appear here."
                    } else {
                        "No session mentions “$query”."
                    },
                    modifier = Modifier.padding(top = 48.dp),
                )
            }

            is RecordingsUiState.Error -> HeaderThen(query, onQueryChange) {
                EmptyState(
                    title = "Couldn't load recordings",
                    body = state.reason,
                    ctaLabel = "Try again",
                    onCta = onRefresh,
                    modifier = Modifier.padding(top = 48.dp),
                )
            }

            is RecordingsUiState.Loaded -> LoadedList(
                state = state,
                query = query,
                onQueryChange = onQueryChange,
                onOpen = onOpen,
                onLoadMore = onLoadMore,
            )
        }
    }
}

@Composable
private fun LoadedList(
    state: RecordingsUiState.Loaded,
    query: String,
    onQueryChange: (String) -> Unit,
    onOpen: (SessionId) -> Unit,
    onLoadMore: () -> Unit,
) {
    val listState = rememberLazyListState()
    val items = state.items
    val groups = remember(items) { groupByRecency(items) }

    // Fire load-more when the last item scrolls into view and the list can
    // still grow. Suppressed while an inline loadError is showing — the
    // error row's Retry is the manual trigger then, so scrolling down to
    // read the error doesn't silently re-fire the request that just failed.
    val shouldLoadMore by remember(items.size, state.canLoadMore, state.loadError) {
        derivedStateOf {
            if (!state.canLoadMore || state.loadError != null) {
                false
            } else {
                val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
                last >= listState.layoutInfo.totalItemsCount - 2
            }
        }
    }
    LaunchedEffect(shouldLoadMore) { if (shouldLoadMore) onLoadMore() }

    LazyColumn(
        state = listState,
        modifier = Modifier.fillMaxSize().statusBarsPadding(),
        contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 28.dp),
    ) {
        item(key = "header") { RecordingsHeader(query, onQueryChange) }

        groups.forEach { group ->
            item(key = "label-${group.label}") {
                SenseGroupLabel(group.label, Modifier.padding(top = 26.dp, bottom = 12.dp))
            }
            items(
                count = group.items.size,
                key = { index -> group.items[index].id.value },
            ) { index ->
                val session = group.items[index]
                SessionCard(
                    session = session,
                    onClick = { onOpen(session.id) },
                    modifier = Modifier.padding(bottom = 10.dp),
                )
            }
        }

        if (state.loadError != null) {
            item(key = "load-error") {
                InlineError(
                    message = state.loadError.toDisplayMessage(),
                    onRetry = onLoadMore,
                )
            }
        }
    }
}

@Composable
private fun RecordingsHeader(query: String, onQueryChange: (String) -> Unit) {
    SenseScreenHeader(eyebrow = "Recordings", hero = "Everything it heard")
    SenseSearchField(
        value = query,
        onValueChange = onQueryChange,
        placeholder = "Search transcripts",
        modifier = Modifier.padding(top = 16.dp),
    )
}

/** Header + search field with arbitrary content below — the empty and error
 *  branches keep the search box so the user can clear their query. */
@Composable
private fun HeaderThen(
    query: String,
    onQueryChange: (String) -> Unit,
    content: @Composable () -> Unit,
) {
    Column(
        Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .padding(horizontal = 20.dp),
    ) {
        RecordingsHeader(query, onQueryChange)
        content()
    }
}

@Composable
private fun SessionCard(
    session: SessionSummary,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    SenseCard(modifier = modifier, onClick = onClick) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Text(
                text = sessionTitle(session),
                style = MaterialTheme.typography.titleSmall,
                color = colors.ink,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Text(
                text = formatRelative(System.currentTimeMillis(), session.startedAt.toEpochMilli()),
                style = MaterialTheme.typography.labelMedium,
                color = colors.greyLight,
            )
        }
        Text(
            text = session.preview.ifBlank { "No transcript yet." },
            style = MaterialTheme.typography.bodyMedium,
            color = colors.inkMuted,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 6.dp),
        )
        Text(
            text = "${formatHmMs(session.durationMs)} · ${session.transcriptCount} segments",
            style = MaterialTheme.typography.bodySmall,
            color = colors.greyLight,
            modifier = Modifier.padding(top = 10.dp),
        )
    }
}

@Composable
private fun InlineError(message: String, onRetry: () -> Unit) {
    Column(Modifier.fillMaxWidth().padding(top = 16.dp)) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodySmall,
            color = SenseTheme.colors.grey,
        )
        SecondaryButton(
            label = "Retry",
            onClick = onRetry,
            modifier = Modifier.padding(top = 10.dp),
        )
    }
}

@Composable
private fun Centered(content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { content() }
}

/** A recency bucket and its sessions, in the comp's Today/Yesterday/Earlier
 *  order. Empty buckets are dropped. */
internal data class SessionGroup(val label: String, val items: List<SessionSummary>)

/**
 * Bucket sessions by start time relative to now. The server returns them
 * newest-first and carries no grouping of its own, so the split is done
 * here rather than adding a field to [SessionSummary].
 */
internal fun groupByRecency(
    sessions: List<SessionSummary>,
    nowMs: Long = System.currentTimeMillis(),
): List<SessionGroup> {
    val dayMs = 24 * 60 * 60 * 1000L
    val today = mutableListOf<SessionSummary>()
    val yesterday = mutableListOf<SessionSummary>()
    val earlier = mutableListOf<SessionSummary>()
    sessions.forEach { session ->
        val age = nowMs - session.startedAt.toEpochMilli()
        when {
            age < dayMs -> today
            age < 2 * dayMs -> yesterday
            else -> earlier
        }.add(session)
    }
    return listOf(
        SessionGroup("Today", today),
        SessionGroup("Yesterday", yesterday),
        SessionGroup("Earlier", earlier),
    ).filter { it.items.isNotEmpty() }
}
