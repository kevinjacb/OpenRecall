package com.sense.relay.ui.nav

import com.sense.relay.domain.model.SessionId

/**
 * The closed set of in-app screens. Sealed so the type-checker enforces
 * exhaustiveness in `when` blocks. The `route` is what Nav Compose stores
 * in the back stack — pinning these strings as named members keeps the
 * wire format in one place.
 */
sealed interface Destination {
    val route: String

    data object Home : Destination { override val route = "home" }
    data object Recordings : Destination { override val route = "recordings" }
    data object Device : Destination { override val route = "device" }
    data object Settings : Destination { override val route = "settings" }

    /** Session detail screen — per-session transcript timeline. The id is
     *  encoded in the route so deep-links can address a specific session. */
    data class SessionDetail(val id: SessionId) : Destination {
        override val route: String = "session/${id.value}"
    }

    /** Gateway back to SetupActivity. Used by Settings to re-provision; the
     *  actual Activity transition is wired in Phase 7. */
    data object Setup : Destination { override val route = "setup" }
}
