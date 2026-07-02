package com.sense.relay.ui

import android.annotation.SuppressLint
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.ParcelUuid
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import com.sense.relay.RelayService
import com.sense.relay.http.SenseHttpClient
import com.sense.relay.setup.BleProvisioning
import com.sense.relay.setup.DeviceScanner
import com.sense.relay.setup.ProvFrames
import com.sense.relay.setup.ServerApi
import com.sense.relay.setup.SetupViewModel
import com.sense.relay.store.Config
import com.sense.relay.store.ServerConfig
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.io.IOException
import java.util.Collections
import java.util.LinkedHashSet
import java.util.UUID

class SetupActivity : ComponentActivity() {

    private lateinit var scanner: RealDeviceScanner

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        scanner = RealDeviceScanner(this)
        val vm = SetupViewModel(
            serverApi = RealServerApi,
            scanner = scanner,
            onDone = { c ->
                ServerConfig(filesDir).write(c)
                val i = Intent(this, RelayService::class.java)
                    .putExtra("server_url", c.serverUrl)
                    .putExtra("token", c.token)
                startForegroundService(i)
            },
        )
        setContent {
            SenseTheme {
                val step by vm.step.collectAsState()
                SetupScreen(step) { u, t -> lifecycleScope.launch { vm.submitServer(u, t) } }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        if (this::scanner.isInitialized) scanner.close()
    }
}

/** Thin adapter exposing [SenseHttpClient] behind the [ServerApi] contract. */
object RealServerApi : ServerApi {
    override suspend fun health(urlBase: String, token: String): Boolean =
        SenseHttpClient(urlBase, token).health()

    override suspend fun pubkey(urlBase: String, token: String): ByteArray =
        SenseHttpClient(urlBase, token).serverPubkey()
}

/**
 * Real BLE [DeviceScanner] for provisioning. Self-contained: a fresh GATT controller per
 * [connect] (one outstanding op at a time, matching the Android GATT contract), suspend
 * bridging via [CompletableDeferred], and a bounded scan window.
 *
 * On-device BLE is a bench-side validation gap (the emulator has no BLE radio); this class
 * compiles against the Android SDK and is structurally reasonable but is exercised only on
 * real hardware. Mirrors SensorLink's patterns: [SuppressLint], deprecated write/read API
 * behind [Suppress("DEPRECATION")], service-UUID scan filter, MTU 247 before ops.
 */
@SuppressLint("MissingPermission")
class RealDeviceScanner(private val context: Context) : DeviceScanner {

    private val adapter =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter

    private var currentGatt: BluetoothGatt? = null

    override suspend fun scan(): List<String> {
        val scanner = adapter.bluetoothLeScanner ?: return emptyList()
        val found = Collections.synchronizedSet(LinkedHashSet<String>())
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                found.add(result.device.address)
            }
        }
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(PROV_SERVICE)).build()
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        runCatching { scanner.startScan(listOf(filter), settings, callback) }
        try {
            delay(SCAN_WINDOW_MS)
        } finally {
            runCatching { scanner.stopScan(callback) }
        }
        return found.toList()
    }

    override suspend fun connect(address: String): BleProvisioning {
        val device = adapter.getRemoteDevice(address)
            ?: throw IOException("unknown device $address")
        val cb = ProvGattCallback()
        val gatt = device.connectGatt(context, false, cb, BluetoothProfile.GATT)
            ?: throw IOException("connectGatt failed")
        currentGatt = gatt
        withTimeoutOrNull(CONNECT_TIMEOUT_MS) { cb.servicesReady.await() }
            ?: throw IOException("connect/service-discovery timeout")
        return BleProvImpl(gatt, cb).also { cb.impl = it }
    }

    /** Close any outstanding GATT connection (e.g. on activity destroy / wizard finish). */
    fun close() {
        currentGatt?.disconnect()
        currentGatt?.close()
        currentGatt = null
    }

    companion object {
        // Provisioning service + characteristics (firmware config.h)
        val PROV_SERVICE: UUID = UUID.fromString("6e9d0010-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val STATE: UUID = UUID.fromString("6e9d0011-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val SERVER_KEY: UUID = UUID.fromString("6e9d0012-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val FACTORY_RESET: UUID = UUID.fromString("6e9d0013-b5a3-4f6e-9b1a-7c2d5e8f0a10")

        private const val SCAN_WINDOW_MS = 4000L
        private const val CONNECT_TIMEOUT_MS = 10_000L
        private const val OP_TIMEOUT_MS = 5_000L
    }

    /** GATT callback that bridges connection setup + one-at-a-time read/write ops. */
    @SuppressLint("MissingPermission")
    private class ProvGattCallback : BluetoothGattCallback() {
        val servicesReady = CompletableDeferred<Unit>()
        var pendingRead: CompletableDeferred<ByteArray?>? = null
        var pendingWrite: CompletableDeferred<Int>? = null
        var impl: BleProvImpl? = null

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                g.requestMtu(247)  // 32-byte server-key write needs >20-byte payload
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                // Fail any in-flight op so suspenders resume instead of hanging.
                pendingRead?.complete(null)
                pendingWrite?.complete(BluetoothGatt.GATT_FAILURE)
                if (!servicesReady.isCompleted) {
                    servicesReady.completeExceptionally(IOException("disconnected (status=$status)"))
                }
                impl?.close()
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            // Proceed regardless of MTU status; writes will surface failure if too large.
            g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS) servicesReady.complete(Unit)
            else servicesReady.completeExceptionally(IOException("service discovery status=$status"))
        }

        @Deprecated("Deprecated in Java")
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int,
        ) {
            @Suppress("DEPRECATION") val value =
                if (status == BluetoothGatt.GATT_SUCCESS) c.value else null
            pendingRead?.complete(value)
            pendingRead = null
        }

        override fun onCharacteristicWrite(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int,
        ) {
            pendingWrite?.complete(status)
            pendingWrite = null
        }
    }

    /** [BleProvisioning] backed by a connected GATT. Ops are serialized by the caller
     *  (ProvisioningClient issues them sequentially) and by Android's one-op-at-a-time rule. */
    @SuppressLint("MissingPermission")
    private class BleProvImpl(
        private val gatt: BluetoothGatt,
        private val cb: ProvGattCallback,
    ) : BleProvisioning {
        private val service = gatt.getService(PROV_SERVICE)
            ?: throw IOException("provisioning service not found")
        private val stateChar = service.getCharacteristic(STATE)
            ?: throw IOException("STATE characteristic not found")
        private val keyChar = service.getCharacteristic(SERVER_KEY)
            ?: throw IOException("SERVER_KEY characteristic not found")
        private val resetChar = service.getCharacteristic(FACTORY_RESET)
            ?: throw IOException("FACTORY_RESET characteristic not found")

        override suspend fun readState(): Int {
            val deferred = CompletableDeferred<ByteArray?>()
            cb.pendingRead = deferred
            @Suppress("DEPRECATION") run { gatt.readCharacteristic(stateChar) }
            val value = withTimeoutOrNull(OP_TIMEOUT_MS) { deferred.await() }
                ?: throw IOException("read STATE timeout")
            val b = value?.firstOrNull() ?: return ProvFrames.STATE_UNPROVISIONED
            return b.toInt() and 0xFF
        }

        override suspend fun writeServerKey(key: ByteArray) {
            require(key.size == ProvFrames.KEY_LEN) { "server key must be ${ProvFrames.KEY_LEN} bytes" }
            val deferred = CompletableDeferred<Int>()
            cb.pendingWrite = deferred
            keyChar.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            @Suppress("DEPRECATION") run {
                keyChar.value = key
                gatt.writeCharacteristic(keyChar)
            }
            val status = withTimeoutOrNull(OP_TIMEOUT_MS) { deferred.await() }
                ?: throw IOException("write SERVER_KEY timeout")
            if (status != BluetoothGatt.GATT_SUCCESS) {
                // A rejected write (e.g. device already PROVISIONED) surfaces here.
                throw IOException("SERVER_KEY write rejected (status=$status)")
            }
        }

        override suspend fun factoryReset() {
            val deferred = CompletableDeferred<Int>()
            cb.pendingWrite = deferred
            resetChar.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            @Suppress("DEPRECATION") run {
                resetChar.value = ProvFrames.FACTORY_RESET_MAGIC
                gatt.writeCharacteristic(resetChar)
            }
            withTimeoutOrNull(OP_TIMEOUT_MS) { deferred.await() }
                ?: throw IOException("write FACTORY_RESET timeout")
        }

        fun close() {
            runCatching { gatt.disconnect() }
            runCatching { gatt.close() }
        }
    }
}

