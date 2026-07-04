package com.sense.relay.ui.design

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.sense.relay.core.model.PagedResult
import com.sense.relay.core.ui.Spacing

/**
 * A "header + LazyColumn + automatic Loading/Empty/Error rendering" wrapper.
 * The brief notes the type is in place for Phase 5; no current screen
 * uses it yet — RecordingsScreen (Phase 5) is the first caller. Today it
 * compiles, the type machinery works, and no business logic has crept in.
 *
 * Why a wrapper and not just `when (state) { ... }` in the caller: the
 * empty/loading/error UI is shared across every list screen in the app,
 * so centralising it here means a tweak to the empty illustration shows
 * up in 4 places for the price of 1. The wrapper is dumb: it knows the
 * three branches, the caller knows what to render in [Loaded].
 */
@Composable
fun <T> ListSection(
    title: String,
    state: PagedResult<T>,
    modifier: Modifier = Modifier,
    emptyTitle: String = "Nothing here yet",
    emptyBody: String = "When new data arrives, it shows up here.",
    errorTitle: String = "Couldn't load",
    errorBody: String = "Check your connection and try again.",
    onRetry: (() -> Unit)? = null,
    contentPadding: PaddingValues = PaddingValues(Spacing.md),
    items: @Composable LazyListScope.() -> Unit = {},
) {
    Column(modifier = modifier.fillMaxSize()) {
        SectionHeader(title = title, modifier = Modifier.padding(horizontal = Spacing.md, vertical = Spacing.sm))
        when (state) {
            is PagedResult.Loading -> LoadingBlock()
            is PagedResult.Error -> ErrorBlock(errorTitle, errorBody, onRetry)
            is PagedResult.Exhausted, is PagedResult.Page -> {
                val listItems = (state as? PagedResult.Page<T>)?.items.orEmpty()
                if (listItems.isEmpty()) {
                    EmptyBlock(emptyTitle, emptyBody)
                } else {
                    LazyColumn(
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = contentPadding,
                        verticalArrangement = Arrangement.spacedBy(Spacing.sm),
                    ) {
                        items(listItems) { item ->
                            // Caller is responsible for the row composable;
                            // we only own the wrapper. Phase 5 supplies the
                            // real row binding (SessionSummary -> SessionRow).
                            Text(text = item.toString(), style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun LoadingBlock() {
    Box(
        modifier = Modifier.fillMaxSize().padding(Spacing.lg),
        contentAlignment = Alignment.Center,
    ) {
        CircularProgressIndicator()
    }
}

@Composable
private fun EmptyBlock(title: String, body: String) {
    Box(
        modifier = Modifier.fillMaxSize().padding(Spacing.lg),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(title, style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurface)
            Text(
                body,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = Spacing.xs),
            )
        }
    }
}

@Composable
private fun ErrorBlock(title: String, body: String, onRetry: (() -> Unit)?) {
    Box(
        modifier = Modifier.fillMaxSize().padding(Spacing.lg),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(title, style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurface)
            Text(
                body,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = Spacing.xs),
            )
            if (onRetry != null) {
                TextButton(onClick = onRetry, modifier = Modifier.padding(top = Spacing.sm)) {
                    Text("Retry")
                }
            }
        }
    }
}
