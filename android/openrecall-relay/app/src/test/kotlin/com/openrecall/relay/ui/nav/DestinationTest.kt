package com.openrecall.relay.ui.nav

import com.openrecall.relay.domain.model.SegmentId
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

    @Test fun segmentDetailRouteEmbedsId() {
        val s = Destination.SegmentDetail(SegmentId("abc"))
        assertEquals("segment/abc", s.route)
    }

    @Test fun segmentDetailRouteEncodesTheColon() {
        // Ids are "<session_id>:<seq>". The colon is percent-encoded so the
        // route is a single unambiguous path segment.
        val s = Destination.SegmentDetail(SegmentId("sess-1:412"))
        assertEquals("segment/sess-1%3A412", s.route)
    }

    @Test fun segmentDetailRouteIsDeterministic() {
        // Two equal ids → two equal routes. Required for saved-state and
        // back-stack equality.
        val a = Destination.SegmentDetail(SegmentId("xyz:1"))
        val b = Destination.SegmentDetail(SegmentId("xyz:1"))
        assertEquals(a.route, b.route)
    }

    @Test fun differentIdsGiveDifferentRoutes() {
        val a = Destination.SegmentDetail(SegmentId("abc:1"))
        val b = Destination.SegmentDetail(SegmentId("def:1"))
        assertNotEquals(a.route, b.route)
    }

    @Test fun setupRoute() {
        // Setup is a gateway back to SetupActivity — declared here so the nav
        // graph has a known target when we wire MainActivity → Setup in Phase 7.
        assertEquals("setup", Destination.Setup.route)
    }
}
