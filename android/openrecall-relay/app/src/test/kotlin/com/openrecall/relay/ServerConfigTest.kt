package com.openrecall.relay
import com.openrecall.relay.store.ServerConfig
import com.openrecall.relay.store.Config
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ServerConfigTest {
    @Test
    fun roundTripsConfig() = runTest {
        val tmp = File(System.getProperty("java.io.tmpdir"), "sc_${System.nanoTime()}")
        val cfg = ServerConfig(tmp)  // ctor takes a File dir for testability
        cfg.write(Config("wss://x:8765", "tok", "AA:BB", true))
        val read = cfg.read()
        assertEquals("wss://x:8765", read.serverUrl)
        assertEquals("tok", read.token)
        assertEquals("AA:BB", read.deviceAddress)
        assertTrue(read.provisioned)
        cfg.clear()
        assertEquals("", cfg.read().serverUrl)
    }

    @Test
    fun roundTripsGatewayPort() = runTest {
        val tmp = File(System.getProperty("java.io.tmpdir"), "sc_gp_${System.nanoTime()}")
        val cfg = ServerConfig(tmp)
        cfg.write(Config("http://h:8766", "tok", "AA:BB", true, gatewayPort = 8765))
        val read = cfg.read()
        assertEquals(8765, read.gatewayPort)
        // Clearing the field persists null (the key is removed, not left stale).
        cfg.write(Config("http://h:8766", "tok", "AA:BB", true, gatewayPort = null))
        assertEquals(null, cfg.read().gatewayPort)
        cfg.clear()
    }
}