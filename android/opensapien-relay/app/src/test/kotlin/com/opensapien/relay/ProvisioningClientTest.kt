package com.opensapien.relay
import com.opensapien.relay.setup.BleProvisioning
import com.opensapien.relay.setup.ProvisioningClient
import com.opensapien.relay.setup.ProvFrames
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class ProvisioningClientTest {
    private class FakeBle(var state: Int = 0) : BleProvisioning {
        var writtenKey: ByteArray? = null
        var resetCount = 0
        override suspend fun readState(): Int = state
        override suspend fun writeServerKey(key: ByteArray) {
            require(key.size == 32)
            require(state == 0) { "rejected: already provisioned" }
            writtenKey = key; state = 1
        }
        override suspend fun factoryReset() { resetCount++; state = 0 }
    }

    @Test fun magicIsFourA5s() {
        assertArrayEquals(byteArrayOf(0xA5.toByte(), 0xA5.toByte(), 0xA5.toByte(), 0xA5.toByte()), ProvFrames.FACTORY_RESET_MAGIC)
    }

    @Test fun provisionsWhenUnprovisioned() = runTest {
        val ble = FakeBle(0)
        val key = ByteArray(32) { (it + 1).toByte() }
        val final = ProvisioningClient(ble).ensureProvisioned(key)
        assertEquals(1, final)
        assertArrayEquals(key, ble.writtenKey)
    }

    @Test fun noopWhenAlreadyProvisioned() = runTest {
        val ble = FakeBle(1)
        val final = ProvisioningClient(ble).ensureProvisioned(ByteArray(32))
        assertEquals(1, final)
        assertEquals(null, ble.writtenKey)  // did not re-write
    }
}