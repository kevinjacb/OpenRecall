package com.sense.relay.ui.nav

import com.sense.relay.domain.model.SessionId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotEquals

/**
 * Routes are the wire format of the navigation graph: a small change here
 * breaks deep-links, saved state, and tests. Pin the exact strings and the
 * SessionDetail construction so this is a deliberate API.
 */
class DestinationTest {

    @Test fun homeRoute() {
        assertEquals("home", Destination.Home.route)
    }

    @Test fun recordingsRoute() {
        assertEquals("recordings", Destination.Recordings.route)
    }

    @Test fun deviceRoute() {
        assertEquals("device", Destination.Device.route)
    }

    @Test fun settingsRoute() {
        assertEquals("settings", Destination.Settings.route)
    }

    @Test fun sessionDetailRouteEmbedsId() {
        val s = Destination.SessionDetail(SessionId("abc"))
        assertEquals("session/abc", s.route)
    }

    @Test fun sessionDetailRouteIsDeterministic() {
        // Two equal ids → two equal routes. Required for saved-state and
        // back-stack equality.
        val a = Destination.SessionDetail(SessionId("xyz"))
        val b = Destination.SessionDetail(SessionId("xyz"))
        assertEquals(a.route, b.route)
    }

    @Test fun differentIdsGiveDifferentRoutes() {
        val a = Destination.SessionDetail(SessionId("abc"))
        val b = Destination.SessionDetail(SessionId("def"))
        assertNotEquals(a.route, b.route)
    }

    @Test fun setupRoute() {
        // Setup is a gateway back to SetupActivity — declared here so the nav
        // graph has a known target when we wire MainActivity → Setup in Phase 7.
        assertEquals("setup", Destination.Setup.route)
    }
}
