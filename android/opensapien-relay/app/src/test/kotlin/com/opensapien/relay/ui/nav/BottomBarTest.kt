package com.opensapien.relay.ui.nav

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Architectural invariant: the bottom bar has AT MOST 5 tabs, and they are
 * exactly the ones the `design/` comp specifies.
 *
 * The ceiling was 4 while Home carried drill-down cards for Device and
 * Commands. The comp replaced those with a Memories tab — "what it decided
 * to keep" is one of the product's two read surfaces, and reaching it only
 * via a chat refusal made it undiscoverable — and moved the two diagnostic
 * screens behind Settings. Five is the new ceiling; a sixth means the
 * navigation needs redesigning, not another slot.
 *
 * These assertions read the real [mainDestinations] list rather than a
 * hand-copied one, so adding a tab fails here instead of passing vacuously.
 */
class BottomBarTest {

    @Test
    fun `bottom bar ceiling is 5 tabs (INV-13)`() {
        assertTrue(
            "bottom bar has ${mainDestinations.size} tabs; the ceiling is 5",
            mainDestinations.size <= 5,
        )
    }

    @Test
    fun `bottom bar tabs are the five design destinations, in order`() {
        assertEquals(
            listOf(
                Destination.Home,
                Destination.Recordings,
                Destination.Memory,
                Destination.Chat,
                Destination.Settings,
            ),
            mainDestinations.map { it.destination },
        )
    }

    @Test
    fun `diagnostic surfaces are not bottom bar entries`() {
        // Device and Commands are pushed from Settings, not tabbed.
        val tabs = mainDestinations.map { it.destination }.toSet()
        assertFalse(Destination.Device in tabs)
        assertFalse(Destination.Commands in tabs)
    }

    @Test
    fun `every tab has a label`() {
        assertTrue(mainDestinations.all { it.label.isNotBlank() })
    }

    @Test
    fun `atom detail deep link is routable`() {
        assertEquals("memory/atom/a1", Destination.AtomDetail.build("a1").route)
    }
}
