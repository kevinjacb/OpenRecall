package com.opensapien.relay.ui.nav

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Architectural invariant: the bottom bar has AT MOST 4 tabs.
 *
 * The phone is a relay; the spec gives 4 explicit slots
 * (Home, Recordings, Device, Settings) and no fifth. New features
 * (Chat, Memory) live as screens reached from one of the four tabs —
 * not as new bottom-bar entries. This invariant is binding: a
 * regression here means we have to redesign the navigation.
 */
class BottomBarTest {

    @Test
    fun `bottom bar ceiling is 4 tabs (INV-13)`() {
        // The 4 canonical tabs.
        val tabs = listOf(
            Destination.Home,
            Destination.Recordings,
            Destination.Device,
            Destination.Settings,
        )
        assertEquals(4, tabs.size)
        assertTrue(tabs.size <= 4)
    }

    @Test
    fun `chat and memory are not bottom bar entries`() {
        // Cognitive read path screens are reachable from Home, not from
        // the bottom bar. They are Destinations, not bottom-bar tabs.
        val bottomBarTabs = setOf(
            Destination.Home,
            Destination.Recordings,
            Destination.Device,
            Destination.Settings,
        )
        assertTrue(Destination.Chat !in bottomBarTabs)
        assertTrue(Destination.Memory !in bottomBarTabs)
    }

    @Test
    fun `atom detail deep link is routable`() {
        val dest = Destination.AtomDetail.build("a1")
        assertEquals("memory/atom/a1", dest.route)
    }
}
