package com.openrecall.relay.ui.nav

import com.openrecall.relay.domain.model.SegmentId
import java.net.URLEncoder

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

    /** P2-commands user-facing surface — active command lifecycle. */
    data object Commands : Destination { override val route = "commands" }

    data object Settings : Destination { override val route = "settings" }

    /** Cognitive read path: chat with the agent. */
    data object Chat : Destination { override val route = "chat" }

    /** Cognitive read path: browse + search the memory. */
    data object Memory : Destination { override val route = "memory" }

    /**
     * Recording detail — one segment's transcript, memories and audio.
     *
     * The id is `"<session_id>:<seq>"`, so it is URL-encoded into the route:
     * a raw colon in a path segment is legal per RFC 3986 but is exactly the
     * kind of thing that breaks silently in one navigation library version
     * and not the next, and the encode/decode pair costs nothing.
     */
    data class SegmentDetail(val id: SegmentId) : Destination {
        override val route: String = "segment/${URLEncoder.encode(id.value, "UTF-8")}"

        companion object {
            const val ARG_SEGMENT_ID = "segmentId"
            const val ROUTE_TEMPLATE = "segment/{segmentId}"
        }
    }

    /** Atom detail screen — one cited atom, drilled into from a chat chip.
     *  The id is encoded in the route so a chip tap deep-links here. */
    data class AtomDetail(val atomId: String) : Destination {
        override val route: String = "memory/atom/$atomId"
        companion object {
            /**
             * Nav-arg key. Named in one place so the AppNavigation
             * composable() and the route reader stay in sync.
             */
            const val ARG_ATOM_ID = "atomId"
            /**
             * Route template, used by [com.openrecall.relay.ui.nav.AppNavigation]
             * to register the composable() (the templated path with the
             * nav-arg placeholder).
             */
            const val ROUTE_TEMPLATE = "memory/atom/{atomId}"
            fun build(atomId: String) = AtomDetail(atomId)
        }
    }

    /** Gateway back to SetupActivity. Used by Settings to re-provision; the
     *  actual Activity transition is wired in Phase 7. */
    data object Setup : Destination { override val route = "setup" }
}
