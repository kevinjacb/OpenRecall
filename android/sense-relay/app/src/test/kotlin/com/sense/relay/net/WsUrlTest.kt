package com.sense.relay.net

import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Pins [wsGatewayUrl]: the relay's WebSocket URL is the provisioned HTTP host
 * on the server-reported gateway port (NOT the HTTP port), with the scheme
 * swapped. This is the regression test for the "Expected HTTP 101 response"
 * bug, where the phone reused the HTTP port for the WS upgrade.
 */
class WsUrlTest {

    @Test fun usesGatewayPortOnTheProvisionedHost() {
        // The bug case: HTTP API on :8766, WS gateway on :8765. The phone
        // provisioned the HTTP URL; the server reported gatewayPort=8765.
        assertEquals("ws://192.168.1.20:8765", wsGatewayUrl("http://192.168.1.20:8766", 8765))
    }

    @Test fun httpsUpgradesToWss() {
        assertEquals("wss://host.example:8765", wsGatewayUrl("https://host.example:443", 8765))
    }

    @Test fun gatewayPortNullFallsBackToTheUrlPort() {
        // Back-compat: an older server that doesn't report a gateway port keeps
        // the old scheme-swap behavior (same host:port as the HTTP URL). No
        // worse than before; the fix only activates when a port is reported.
        assertEquals("ws://host:8766", wsGatewayUrl("http://host:8766", null))
    }

    @Test fun emulatorDefaultWsUrlIsPreserved() {
        // The RelayService emulator-loopback default is already ws://…:8765.
        // No https?:// prefix to swap, and gatewayPort null → keep its port.
        assertEquals("ws://10.0.2.2:8765", wsGatewayUrl("ws://10.0.2.2:8765", null))
    }

    @Test fun noPortInUrlAndNoGatewayPortOmitsPort() {
        assertEquals("ws://host", wsGatewayUrl("http://host", null))
        assertEquals("wss://host", wsGatewayUrl("https://host", null))
    }

    @Test fun noPortInUrlUsesGatewayPort() {
        assertEquals("ws://host:8765", wsGatewayUrl("http://host", 8765))
    }

    @Test fun dropsPathAndQuery() {
        // The HTTP API path is irrelevant to the WS gateway.
        assertEquals("ws://host:8765", wsGatewayUrl("http://host:8766/status", 8765))
        assertEquals("ws://host:8765", wsGatewayUrl("http://host:8766/sessions?limit=20", 8765))
    }
}