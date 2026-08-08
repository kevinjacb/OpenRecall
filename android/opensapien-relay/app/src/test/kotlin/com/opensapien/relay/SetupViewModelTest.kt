package com.opensapien.relay
import com.opensapien.relay.setup.*
import com.opensapien.relay.store.Config
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.flow.first
import org.junit.Assert.*
import org.junit.Test

class SetupViewModelTest {
    private class FakeServer(val ok: Boolean = true, val gwPort: Int? = 8765) : ServerApi {
        override suspend fun health(urlBase: String, token: String) = ok
        override suspend fun pubkey(urlBase: String, token: String) =
            if (ok) ByteArray(32) { (it + 1).toByte() } else throw SecurityException("401")
        override suspend fun gatewayPort(urlBase: String, token: String) = gwPort
    }
    private class FakeScanner(val addrs: List<String> = listOf("AA")) : DeviceScanner {
        var connected: String? = null
        override suspend fun scan() = addrs
        override suspend fun connect(address: String): BleProvisioning {
            connected = address
            return object : BleProvisioning {
                var st = 0
                override suspend fun readState() = st
                override suspend fun writeServerKey(key: ByteArray) { st = 1 }
                override suspend fun factoryReset() { st = 0 }
            }
        }
    }

    @Test fun happyPathGoesEnterServerToDone() = runTest {
        var saved: Config? = null
        val vm = SetupViewModel(FakeServer(), FakeScanner()) { saved = it }
        assertEquals(SetupStep.EnterServer::class, vm.step.value::class)
        vm.submitServer("wss://x:8766", "tok")
        assertTrue(vm.step.value is SetupStep.Done)
        assertNotNull(saved)
        assertEquals("wss://x:8766", saved!!.serverUrl)
        assertTrue(saved!!.provisioned)
        // The server-reported WS gateway port is captured into the persisted
        // config so the relay can derive its WebSocket URL (separate port from
        // the HTTP API). This is the fix for the "Expected HTTP 101 response"
        // bug, where the relay reused the HTTP port for the WS upgrade.
        assertEquals(8765, saved!!.gatewayPort)
    }

    @Test fun provisioningSurvivesAGatewayPortFetchThatReturnsNull() = runTest {
        // An older server (or a transient /health hiccup) reports no gateway
        // port. Provisioning must still succeed; the relay falls back to the
        // legacy same-port scheme-swap when gatewayPort is null.
        var saved: Config? = null
        val vm = SetupViewModel(FakeServer(gwPort = null), FakeScanner()) { saved = it }
        vm.submitServer("wss://x:8766", "tok")
        assertTrue(vm.step.value is SetupStep.Done)
        assertNotNull(saved)
        assertEquals(null, saved!!.gatewayPort)
    }

    @Test fun badTokenStaysOnEnterServerWithError() = runTest {
        val vm = SetupViewModel(FakeServer(ok = false), FakeScanner()) {}
        vm.submitServer("wss://x:8766", "wrong")
        val step = vm.step.value as SetupStep.EnterServer
        assertNotNull(step.error)
    }
}