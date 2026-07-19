package com.sense.relay.ui.nav

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.vector.ImageVector

/**
 * The 4 main destinations. Device and Commands are reachable from
 * Home (drill-down cards); they are not bar tabs.
 *
 * Order: Home, Recordings, Chat, Settings — most-used first,
 * "settings where I look for it" last. Chat sits between
 * Recordings and Settings because it is the user-facing ask surface
 * (the cognitive read path); Device + Commands are admin surfaces
 * hidden behind Home drill-downs.
 *
 * **Icon note:** the core `androidx.compose.material.icons.filled` set
 * only ships a small subset of Material Icons. We use icons that are
 * available in the core set today (`Home`, `List`, `Settings`) and
 * the visual differentiation will be tightened with custom SVGs in
 * a later visual-polish phase. A missing icon today would crash at
 * runtime; this choice is the boring-but-correct one.
 */
private val mainDestinations: List<MainTab> = listOf(
    MainTab(Destination.Home, "Home", Icons.Filled.Home),
    MainTab(Destination.Recordings, "Recordings", Icons.AutoMirrored.Filled.List),
    // Icons.AutoMirrored.Filled.Send (paper plane) is reused for
    // the Chat tab so the icon matches the input bar's send
    // affordance; the core icon set does not include a dedicated
    // Chat icon.
    MainTab(Destination.Chat, "Chat", Icons.AutoMirrored.Filled.Send),
    MainTab(Destination.Settings, "Settings", Icons.Filled.Settings),
)

private data class MainTab(
    val destination: Destination,
    val label: String,
    val icon: ImageVector,
)

/**
 * Bottom navigation bar. Accepts the current route so the active tab is
 * highlighted; the parent owns the actual NavController (so back-stack
 * and pop-up-to are managed in one place).
 */
@Composable
fun BottomBar(
    currentRoute: String?,
    onSelect: (Destination) -> Unit,
) {
    NavigationBar(
        containerColor = MaterialTheme.colorScheme.surface,
        contentColor = MaterialTheme.colorScheme.onSurface,
    ) {
        mainDestinations.forEach { tab ->
            val selected = currentRoute == tab.destination.route
            NavigationBarItem(
                selected = selected,
                onClick = { onSelect(tab.destination) },
                icon = { Icon(tab.icon, contentDescription = tab.label) },
                label = { Text(tab.label) },
                colors = NavigationBarItemDefaults.colors(
                    selectedIconColor = MaterialTheme.colorScheme.onPrimary,
                    indicatorColor = MaterialTheme.colorScheme.primary,
                    unselectedIconColor = MaterialTheme.colorScheme.onSurfaceVariant,
                    selectedTextColor = MaterialTheme.colorScheme.onSurface,
                    unselectedTextColor = MaterialTheme.colorScheme.onSurfaceVariant,
                ),
            )
        }
    }
}
