package com.sense.relay.ui.design

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable

/**
 * State for [SenseTopBar]. The composable reads this rather than a list of
 * ad-hoc params so a future version can add subtitle, search, overflow
 * menu, etc. without breaking call sites.
 */
data class TopBarState(
    val title: String,
    val onBack: (() -> Unit)? = null,
)

/**
 * The branded top bar. Centered title for a clean monochrome look; back
 * arrow appears only when [TopBarState.onBack] is provided. Colors are
 * pulled from the active MaterialTheme so light/dark switching Just Works.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SenseTopBar(state: TopBarState) {
    CenterAlignedTopAppBar(
        title = { Text(state.title, style = MaterialTheme.typography.titleLarge) },
        navigationIcon = {
            if (state.onBack != null) {
                IconButton(onClick = state.onBack) {
                    Icon(
                        imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                        contentDescription = "Back",
                    )
                }
            }
        },
        colors = TopAppBarDefaults.centerAlignedTopAppBarColors(
            containerColor = MaterialTheme.colorScheme.surface,
            titleContentColor = MaterialTheme.colorScheme.onSurface,
            navigationIconContentColor = MaterialTheme.colorScheme.onSurface,
        ),
    )
}
