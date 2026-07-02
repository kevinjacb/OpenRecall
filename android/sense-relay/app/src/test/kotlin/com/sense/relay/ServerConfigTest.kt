package com.sense.relay
import com.sense.relay.store.ServerConfig
import com.sense.relay.store.Config
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
}