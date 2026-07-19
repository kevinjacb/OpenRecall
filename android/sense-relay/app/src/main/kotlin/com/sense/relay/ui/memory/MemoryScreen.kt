package com.sense.relay.ui.memory

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * Memory screen. STUB for this slice — the real MemoryScreen
 * (browse / search atoms) is its own design -> plan -> implement
 * cycle. Reachable from ChatScreen's refuse-link "browse memory
 * directly" button.
 */
@Composable
fun MemoryScreen(modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Memory"))
        EmptyState(
            title = "Memory",
            body = "Browse and search your memories — coming soon.",
            modifier = Modifier
                .fillMaxSize()
                .testTag("memory_empty"),
        )
    }
}
