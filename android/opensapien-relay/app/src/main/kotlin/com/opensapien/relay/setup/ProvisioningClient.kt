package com.opensapien.relay.setup

object ProvFrames {
    const val STATE_UNPROVISIONED = 0
    const val STATE_PROVISIONED = 1
    val FACTORY_RESET_MAGIC: ByteArray = byteArrayOf(0xA5.toByte(), 0xA5.toByte(), 0xA5.toByte(), 0xA5.toByte())
    const val KEY_LEN = 32
}

interface BleProvisioning {
    suspend fun readState(): Int
    suspend fun writeServerKey(key: ByteArray)
    suspend fun factoryReset()
}

class ProvisioningClient(private val ble: BleProvisioning) {
    suspend fun ensureProvisioned(serverPubkey: ByteArray): Int {
        require(serverPubkey.size == ProvFrames.KEY_LEN) { "server pubkey must be 32 bytes" }
        val s = ble.readState()
        if (s == ProvFrames.STATE_UNPROVISIONED) {
            ble.writeServerKey(serverPubkey)
        }
        return ble.readState()
    }
}