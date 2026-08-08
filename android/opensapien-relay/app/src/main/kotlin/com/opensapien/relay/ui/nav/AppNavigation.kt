package com.opensapien.relay.ui.nav

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
import com.opensapien.relay.ui.atom.AtomDetailRoute
import com.opensapien.relay.ui.chat.ChatRoute
import com.opensapien.relay.ui.commands.CommandsRoute
import com.opensapien.relay.ui.device.DeviceRoute
import com.opensapien.relay.ui.home.HomeRoute
import com.opensapien.relay.ui.memory.MemoryRoute
import com.opensapien.relay.ui.recordings.RecordingsRoute
import com.opensapien.relay.ui.recordings.SessionDetailRoute
import com.opensapien.relay.ui.settings.SettingsRoute

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
    onReconfigure: () -> Unit,
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
            composable(Destination.Home.route) {
                HomeRoute(
                    onOpenDevice = {
                        navController.navigate(Destination.Device.route)
                    },
                    onOpenCommands = {
                        navController.navigate(Destination.Commands.route)
                    },
                )
            }
            composable(Destination.Recordings.route) {
                RecordingsRoute(
                    onOpen = { id -> navController.navigate(Destination.SessionDetail(id).route) },
                )
            }
            // Device + Commands are not in the bottom bar (4-tab
            // layout, INV-13 compliant); Home has drill-down cards
            // that navigate here. The composable() entries are kept
            // so the back stack works: Home -> Device -> back returns
            // to Home.
            composable(Destination.Device.route) { DeviceRoute() }
            composable(Destination.Commands.route) { CommandsRoute() }
            // P2-answers user-facing surface (ChatScreen). Chip taps
            // deep-link to AtomDetail; the refuse-link deep-links to
            // Memory.
            composable(Destination.Chat.route) {
                ChatRoute(
                    onOpenAtom = { id ->
                        navController.navigate(Destination.AtomDetail.build(id).route)
                    },
                    onOpenMemory = {
                        navController.navigate(Destination.Memory.route)
                    },
                )
            }
            // Memory — stub reachable from Chat's refuse-link.
            // The full MemoryScreen is a follow-up slice.
            composable(Destination.Memory.route) { MemoryRoute() }
            composable(Destination.Settings.route) { SettingsRoute(onReconfigure) }
            // AtomDetail — stub reachable from Chat's chip-tap.
            // The full AtomDetailScreen is a follow-up slice.
            composable(
                route = Destination.AtomDetail.ROUTE_TEMPLATE,  // "memory/atom/{atomId}"
                arguments = listOf(
                    navArgument(Destination.AtomDetail.ARG_ATOM_ID) {
                        type = NavType.StringType
                    },
                ),
            ) { backStackEntry ->
                val atomId = backStackEntry.arguments
                    ?.getString(Destination.AtomDetail.ARG_ATOM_ID)
                if (atomId == null) {
                    StubScreen("Atom not found")
                } else {
                    AtomDetailRoute(
                        atomId = atomId,
                        onBack = { navController.popBackStack() },
                    )
                }
            }
            // SessionDetail: the route has a path segment for the id; the
            // framework parses it via [SessionIdNavType]. The screen reads the
            // id out of `backStackEntry.arguments` and pops back on the back
            // arrow.
            composable(
                route = "session/{sessionId}",
                arguments = listOf(
                    navArgument("sessionId") { type = SessionIdNavType },
                ),
            ) { backStackEntry ->
                val args = backStackEntry.arguments
                val id = args?.let { SessionIdNavType[it, "sessionId"] }
                if (id == null) {
                    StubScreen("Session not found")
                } else {
                    SessionDetailRoute(id = id, onBack = { navController.popBackStack() })
                }
            }
        }
    }
}

private val MAIN_ROUTES = setOf(
    Destination.Home.route,
    Destination.Recordings.route,
    Destination.Chat.route,
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
