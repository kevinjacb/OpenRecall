package com.sense.relay.ui.memory

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/**
 * Route for the Memory screen. STUB — no ViewModel yet. The real
 * MemoryViewModel (which already exists at ui/memory/MemoryViewModel.kt)
 * is wired in the real-MemoryScreen slice. This stub exists so the
 * Chat refuse-link has a navigable target.
 */
@Composable
fun MemoryRoute(modifier: Modifier = Modifier) {
    MemoryScreen(modifier = modifier)
}
