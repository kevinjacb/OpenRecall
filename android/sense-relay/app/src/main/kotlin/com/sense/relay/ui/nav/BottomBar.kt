package com.sense.relay.ui.nav

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Info
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
 * The 4 main destinations. The "Live" tab is intentionally omitted (per
 * the spec — the phone is a relay, no audio to visualise) and replaced
 * by [Destination.Device] (BLE + server status) since users will care
 * about that more than an empty live tab.
 *
 * Order matters: Home, Recordings, Device, Settings is the spec's
 * reading order — most-used first, "settings where I look for it" last.
 *
 * **Icon note:** the core `androidx.compose.material.icons.filled` set
 * only ships a small subset of Material Icons. We use icons that are
 * available in the core set today (`Home`, `List`, `Info`, `Settings`)
 * and the visual differentiation will be tightened with custom SVGs in
 * a later visual-polish phase. A missing icon today would crash at
 * runtime; this choice is the boring-but-correct one.
 */
private val mainDestinations: List<MainTab> = listOf(
    MainTab(Destination.Home, "Home", Icons.Filled.Home),
    MainTab(Destination.Recordings, "Recordings", Icons.AutoMirrored.Filled.List),
    MainTab(Destination.Device, "Device", Icons.Filled.Info),
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
