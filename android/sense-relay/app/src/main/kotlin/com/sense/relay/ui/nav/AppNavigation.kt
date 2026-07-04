package com.sense.relay.ui.nav

import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument

/**
 * Single-Activity nav graph. The 4 main destinations are tied to the
 * [BottomBar]; SessionDetail is reachable but not in the bar (it's
 * pushed onto Home/Recordings). Each destination is currently a stub
 * `Text(...)` — real screens land in Phase 4+.
 *
 * **Typed destinations note:** the brief's preferred form is
 * `composable<Destination.SessionDetail>` (Nav Compose 2.8 typed
 * destinations). We fall back to the string-route form here because
 * `SessionId` is a value class and the typed API requires
 * `@Serializable` on the route, which would pull kotlinx-serialization
 * into the navigation model. Phase 5 (the first phase that needs to
 * navigate to a specific session) can adopt typed destinations once a
 * real navigation graph exists; the rest of the API (route strings,
 * `SessionIdNavType`) is already in place for that migration.
 */
@Composable
fun AppNavigation(
    navController: NavHostController = rememberNavController(),
) {
    Scaffold(
        bottomBar = {
            val backStackEntry by navController.currentBackStackEntryAsState()
            val route = backStackEntry?.destination?.route
            if (route in MAIN_ROUTES) {
                BottomBar(
                    currentRoute = route,
                    onSelect = { dest ->
                        navController.navigate(dest.route) {
                            launchSingleTop = true
                            restoreState = true
                            popUpTo(Destination.Home.route) {
                                saveState = true
                            }
                        }
                    },
                )
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = Destination.Home.route,
            modifier = Modifier.padding(padding),
        ) {
            composable(Destination.Home.route) { StubScreen("Home — coming soon") }
            composable(Destination.Recordings.route) { StubScreen("Recordings — coming soon") }
            composable(Destination.Device.route) { StubScreen("Device — coming soon") }
            composable(Destination.Settings.route) { StubScreen("Settings — coming soon") }
            // SessionDetail: the route has a path segment for the id;
            // the framework parses it via [SessionIdNavType]. Phase 5
            // will turn this stub into a real screen reading the id
            // out of `backStackEntry.arguments`.
            composable(
                route = "session/{sessionId}",
                arguments = listOf(
                    navArgument("sessionId") { type = SessionIdNavType },
                ),
            ) {
                StubScreen("Session — coming soon")
            }
        }
    }
}

private val MAIN_ROUTES = setOf(
    Destination.Home.route,
    Destination.Recordings.route,
    Destination.Device.route,
    Destination.Settings.route,
)

@Composable
private fun StubScreen(label: String) {
    Text(
        text = label,
        style = MaterialTheme.typography.titleLarge,
        color = MaterialTheme.colorScheme.onSurface,
        modifier = Modifier.padding(24.dp),
    )
}
