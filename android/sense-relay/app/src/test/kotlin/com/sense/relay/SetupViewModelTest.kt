package com.sense.relay
import com.sense.relay.setup.*
import com.sense.relay.store.Config
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.flow.first
import org.junit.Assert.*
import org.junit.Test

class SetupViewModelTest {
    private class FakeServer(val ok: Boolean = true) : ServerApi {
        override suspend fun health(urlBase: String, token: String) = ok
        override suspend fun pubkey(urlBase: String, token: String) =
            if (ok) ByteArray(32) { (it + 1).toByte() } else throw SecurityException("401")
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
    }

    @Test fun badTokenStaysOnEnterServerWithError() = runTest {
        val vm = SetupViewModel(FakeServer(ok = false), FakeScanner()) {}
        vm.submitServer("wss://x:8766", "wrong")
        val step = vm.step.value as SetupStep.EnterServer
        assertNotNull(step.error)
    }
}