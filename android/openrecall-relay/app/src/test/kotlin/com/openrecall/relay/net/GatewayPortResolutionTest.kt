package com.openrecall.relay.net

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

/**
 * Pins the gateway-port fallback, including the case that used to be
 * impossible: clearing the override.
 */
class GatewayPortResolutionTest {

    @Test fun provisioningExtraWins() {
        assertEquals(8765, resolveGatewayPort(extra = 8765, persisted = 443))
    }

    @Test fun persistedValueIsAdoptedWhenThereIsNoExtra() {
        assertEquals(443, resolveGatewayPort(extra = null, persisted = 443))
    }

    @Test fun clearingTheOverrideActuallyClearsIt() {
        // The bug: `persisted?.let { port = it }` left the previous port in
        // place, so Settings could raise the port but never lower it back to
        // the URL's. Null must propagate.
        assertNull(resolveGatewayPort(extra = null, persisted = null))
    }

    @Test fun aClearedOverrideMakesTheUrlFallBackToItsOwnPort() {
        // The end-to-end consequence, which is the point of the setting:
        // behind a tunnel the phone must dial 443, not the server's bind port.
        val port = resolveGatewayPort(extra = null, persisted = null)
        assertEquals("wss://sense.example.com", wsGatewayUrl("https://sense.example.com", port))
    }

    @Test fun anOverrideIsUsedVerbatimInTheUrl() {
        val port = resolveGatewayPort(extra = null, persisted = 8765)
        assertEquals("wss://box.lan:8765", wsGatewayUrl("https://box.lan", port))
    }
}
