package com.opensapien.relay.ui.nav

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Scaffold
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.ui.atom.AtomDetailRoute
import com.opensapien.relay.ui.chat.ChatRoute
import com.opensapien.relay.ui.commands.CommandsRoute
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.device.DeviceRoute
import com.opensapien.relay.ui.home.HomeRoute
import com.opensapien.relay.ui.memory.MemoryRoute
import com.opensapien.relay.ui.recordings.RecordingsRoute
import com.opensapien.relay.ui.recordings.SessionDetailRoute
import com.opensapien.relay.ui.settings.SettingsRoute

/**
 * Single-Activity nav graph.
 *
 * Five destinations sit in the [BottomBar] — Home, Recordings, Memories,
 * Chat, Settings — matching the `design/` comp. Session detail, atom detail,
 * Device and Commands are push screens: they hide the bar and pop back.
 *
 * [onReconfigure] launches the pairing wizard ([com.opensapien.relay.ui.SetupActivity])
 * through MainActivity's activity-result launcher. It is reachable from two
 * places, deliberately: Home's first-run call to action, and Settings →
 * Reconfigure.
 *
 * **Typed destinations note:** the string-route form is used rather than
 * `composable<Destination.SessionDetail>` because `SessionId` is a value
 * class and the typed API needs `@Serializable` on the route, which would
 * pull kotlinx-serialization into the navigation model.
 */
@Composable
fun AppNavigation(
    onReconfigure: () -> Unit,
    navController: NavHostController = rememberNavController(),
) {
    val colors = SenseTheme.colors

    fun switchTab(dest: Destination) {
        navController.navigate(dest.route) {
            launchSingleTop = true
            restoreState = true
            popUpTo(Destination.Home.route) { saveState = true }
        }
    }

    Scaffold(
        containerColor = colors.canvas,
        bottomBar = {
            val backStackEntry by navController.currentBackStackEntryAsState()
            val route = backStackEntry?.destination?.route
            if (route in MAIN_ROUTES) {
                BottomBar(currentRoute = route, onSelect = ::switchTab)
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = Destination.Home.route,
            modifier = Modifier
                .background(colors.canvas)
                .padding(padding),
        ) {
            composable(Destination.Home.route) {
                HomeRoute(
                    onSetUpDevice = onReconfigure,
                    onOpenSettings = { switchTab(Destination.Settings) },
                    onSeeAllRecordings = { switchTab(Destination.Recordings) },
                    onOpenSession = { id ->
                        navController.navigate(Destination.SessionDetail(id).route)
                    },
                )
            }
            composable(Destination.Recordings.route) {
                RecordingsRoute(
                    onOpen = { id -> navController.navigate(Destination.SessionDetail(id).route) },
                )
            }
            // Memories is a bar tab in the redesign. A chip tap deep-links to
            // the atom detail.
            composable(Destination.Memory.route) {
                MemoryRoute(
                    onOpenAtom = { id ->
                        navController.navigate(Destination.AtomDetail.build(id).route)
                    },
                )
            }
            composable(Destination.Chat.route) {
                ChatRoute(
                    onOpenAtom = { id ->
                        navController.navigate(Destination.AtomDetail.build(id).route)
                    },
                    onOpenMemory = { switchTab(Destination.Memory) },
                )
            }
            composable(Destination.Settings.route) {
                SettingsRoute(
                    onReconfigure = onReconfigure,
                    onOpenDevice = { navController.navigate(Destination.Device.route) },
                    onOpenCommands = { navController.navigate(Destination.Commands.route) },
                )
            }
            // Device + Commands are diagnostic surfaces, not bar tabs. They
            // are pushed from Settings, so back returns there.
            composable(Destination.Device.route) {
                DeviceRoute(onBack = { navController.popBackStack() })
            }
            composable(Destination.Commands.route) {
                CommandsRoute(onBack = { navController.popBackStack() })
            }
            composable(
                route = Destination.AtomDetail.ROUTE_TEMPLATE, // "memory/atom/{atomId}"
                arguments = listOf(
                    navArgument(Destination.AtomDetail.ARG_ATOM_ID) { type = NavType.StringType },
                ),
            ) { backStackEntry ->
                val atomId = backStackEntry.arguments
                    ?.getString(Destination.AtomDetail.ARG_ATOM_ID)
                if (atomId == null) {
                    NotFound("Memory not found", "That memory is no longer available.")
                } else {
                    AtomDetailRoute(atomId = atomId, onBack = { navController.popBackStack() })
                }
            }
            // SessionDetail carries the id as a path segment, parsed by
            // [SessionIdNavType] so deep-links can address one session.
            composable(
                route = "session/{sessionId}",
                arguments = listOf(navArgument("sessionId") { type = SessionIdNavType }),
            ) { backStackEntry ->
                val id = backStackEntry.arguments?.let { SessionIdNavType[it, "sessionId"] }
                if (id == null) {
                    NotFound("Session not found", "That recording is no longer available.")
                } else {
                    SessionDetailRoute(id = id, onBack = { navController.popBackStack() })
                }
            }
        }
    }
}

/** The routes that keep the bottom bar on screen. */
private val MAIN_ROUTES = setOf(
    Destination.Home.route,
    Destination.Recordings.route,
    Destination.Memory.route,
    Destination.Chat.route,
    Destination.Settings.route,
)

@Composable
private fun NotFound(title: String, body: String) {
    Box(
        Modifier.fillMaxSize().background(SenseTheme.colors.canvas),
        contentAlignment = Alignment.Center,
    ) {
        EmptyState(title = title, body = body)
    }
}
