package com.openrecall.relay.ui

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
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
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.ParcelUuid
import android.util.Log
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.openrecall.relay.RelayService
import com.openrecall.relay.ble.SensorLink
import com.openrecall.relay.http.OpenRecallHttpClient
import com.openrecall.relay.setup.BleProvisioning
import com.openrecall.relay.setup.DeviceScanner
import com.openrecall.relay.setup.ProvFrames
import com.openrecall.relay.setup.ServerApi
import com.openrecall.relay.setup.SetupViewModel
import com.openrecall.relay.store.Config
import com.openrecall.relay.store.ServerConfig
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import java.io.IOException
import java.util.Collections
import java.util.LinkedHashSet
import java.util.UUID

class SetupActivity : ComponentActivity() {

    private lateinit var scanner: RealDeviceScanner
    private lateinit var vm: SetupViewModel

    // The BLE scan needs a runtime permission grant: BLUETOOTH_SCAN/BLUETOOTH_CONNECT on
    // Android 12+, or ACCESS_FINE_LOCATION below. The manifest declaration alone grants
    // nothing — without the runtime grant the scanner throws SecurityException (12+) or
    // returns no results (<=11), so the wizard never finds the device even though other BLE
    // apps can. We gate the wizard's "Connect" action on the grant.
    private var pendingConnect: Pair<String, String>? = null

    private val blePermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        val pending = pendingConnect
        pendingConnect = null
        if (grants.values.all { it }) {
            pending?.let { (u, t) -> lifecycleScope.launch { vm.submitServer(u, t) } }
        } else {
            Toast.makeText(this, "Bluetooth permission is required to find the OpenRecall device", Toast.LENGTH_LONG).show()
        }
    }

    private fun requiredBlePermissions(): Array<String> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        else
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)

    private fun hasBlePermission(): Boolean = requiredBlePermissions().all {
        ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
    }

    private fun beginConnect(url: String, token: String) {
        if (hasBlePermission()) {
            lifecycleScope.launch { vm.submitServer(url, token) }
        } else {
            pendingConnect = url to token
            blePermissionLauncher.launch(requiredBlePermissions())
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        scanner = RealDeviceScanner(this)
        // Pre-fill the form with the last-entered server URL + token (persisted on each
        // attempt) so the user doesn't retype them across retries or app restarts.
        val saved = runBlocking { runCatching { ServerConfig(filesDir).read() }.getOrDefault(Config()) }
        vm = SetupViewModel(
            serverApi = RealServerApi,
            scanner = scanner,
            onDone = { c ->
                ServerConfig(filesDir).write(c)
                val i = Intent(this, RelayService::class.java)
                    .putExtra("server_url", c.serverUrl)
                    .putExtra("token", c.token)
                c.gatewayPort?.let { i.putExtra("gateway_port", it) }
                startForegroundService(i)
                // The wizard is always launched for-result from MainActivity
                // (first run from Home's CTA, later from Settings), so it
                // hands the result back and finishes. It never starts
                // MainActivity itself — MainActivity is the launcher.
                setResult(RESULT_OK)
                finish()
            },
            onAttempt = { u, t -> ServerConfig(filesDir).saveCredentials(u, t) },
            initialUrl = saved.serverUrl,
            initialToken = saved.token,
        )
        enableEdgeToEdgeLight()
        setContent {
            OpenRecallTheme {
                val step by vm.step.collectAsState()
                SetupScreen(
                    step = step,
                    onCancel = { finish() },
                    onServer = { u, t -> beginConnect(u, t) },
                )
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        if (this::scanner.isInitialized) scanner.close()
    }

}

/** Thin adapter exposing [OpenRecallHttpClient] behind the [ServerApi] contract. */
object RealServerApi : ServerApi {
    override suspend fun health(urlBase: String, token: String): Boolean =
        OpenRecallHttpClient(urlBase, token).health()

    override suspend fun pubkey(urlBase: String, token: String): ByteArray =
        OpenRecallHttpClient(urlBase, token).serverPubkey()

    override suspend fun gatewayPort(urlBase: String, token: String): Int? =
        runCatching { OpenRecallHttpClient(urlBase, token).gatewayPort() }.getOrNull()
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

            override fun onScanFailed(errorCode: Int) {
                // Surface scan failures instead of failing silently to an empty result.
                Log.w(TAG, "BLE scan failed (errorCode=$errorCode); no devices will be found")
            }
        }
        // The firmware advertises the AUDIO service UUID (6e9d0001) + name "OpenRecall" in its
        // primary advertisement; the provisioning service (6e9d0010) lives in the GATT table
        // only and is discovered after connect. So a UUID-filtered scan must target the
        // *advertised* service — filtering on 6e9d0010 (the old filter) matches nothing.
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(OPENRECALL_SERVICE)).build()
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
        // Transport must be a BluetoothDevice.TRANSPORT_* constant. BluetoothProfile.GATT
        // is a profile id (value 7), not a transport — passing it is a known cause of
        // "registerApp OK, onConnectionStateChange never fires" on Android 10. Use
        // TRANSPORT_LE for a single-mode BLE device like the ESP32.
        val gatt = device.connectGatt(context, false, cb, BluetoothDevice.TRANSPORT_LE)
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
        private const val TAG = "RealDeviceScanner"

        // The service UUID the firmware actually advertises (audio service 6e9d0001).
        // Single source of truth: SensorLink.SERVICE. The setup scan finds the device by this
        // advertised UUID; the provisioning service (PROV_SERVICE) is resolved post-connect.
        private val OPENRECALL_SERVICE: UUID = SensorLink.SERVICE

        // Provisioning service + characteristics (firmware config.h); used post-connect only.
        val PROV_SERVICE: UUID = UUID.fromString("6e9d0010-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val STATE: UUID = UUID.fromString("6e9d0011-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val SERVER_KEY: UUID = UUID.fromString("6e9d0012-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val FACTORY_RESET: UUID = UUID.fromString("6e9d0013-b5a3-4f6e-9b1a-7c2d5e8f0a10")

        private const val SCAN_WINDOW_MS = 10000L
        private const val CONNECT_TIMEOUT_MS = 10_000L
        private const val MTU_TIMEOUT_MS = 3_000L
        private const val OP_TIMEOUT_MS = 5_000L
    }

    /** GATT callback that bridges connection setup + one-at-a-time read/write ops. */
    @SuppressLint("MissingPermission")
    private class ProvGattCallback : BluetoothGattCallback() {
        val servicesReady = CompletableDeferred<Unit>()
        val mtuReady = CompletableDeferred<Unit>()
        var pendingRead: CompletableDeferred<ByteArray?>? = null
        var pendingWrite: CompletableDeferred<Int>? = null
        var impl: BleProvImpl? = null

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            // Always log status + newState: cheap when working, essential when not.
            // status != 0 indicates a GATT-layer error (0x85=GATT_FAILURE, 0x3E=
            // CONN_FAIL_ESTABLISH, 0x87=NOT_CONNECTED, etc.) and explains a connect
            // that never reaches STATE_CONNECTED.
            Log.i(TAG, "onConnectionStateChange status=$status newState=$newState")
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                // Discover services directly on connect. Do NOT gate discovery on the MTU
                // exchange: requestMtu() right after connect can fail to fire onMtuChanged on
                // some stacks, which would leave discoverServices() never called — the
                // "connect/service-discovery timeout" failure. MTU is enlarged after discovery.
                g.discoverServices()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                // Fail any in-flight op so suspenders resume instead of hanging.
                pendingRead?.complete(null)
                pendingWrite?.complete(BluetoothGatt.GATT_FAILURE)
                if (!servicesReady.isCompleted) {
                    servicesReady.completeExceptionally(IOException("disconnected (status=$status)"))
                }
                if (!mtuReady.isCompleted) mtuReady.completeExceptionally(IOException("disconnected"))
                impl?.close()
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            Log.i(TAG, "onServicesDiscovered status=$status")
            if (status == BluetoothGatt.GATT_SUCCESS) {
                // Release the connect-time await immediately. Do NOT also call
                // requestMtu(247) here: the MTU exchange would be in-flight when
                // the caller immediately issues the first read, and on some Android
                // stacks a second GATT op issued before the first completes is
                // rejected (gatt.readCharacteristic returns false, and no
                // onCharacteristicRead callback ever fires — a silent 5s timeout).
                // MTU enlargement is requested later, just before the 32-byte
                // server-key write that actually needs it.
                servicesReady.complete(Unit)
            } else {
                servicesReady.completeExceptionally(IOException("service discovery status=$status"))
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            // Proceed regardless of the negotiated size; an oversized write surfaces a clear
            // length-status error rather than a silent discovery hang.
            mtuReady.complete(Unit)
        }

        @Deprecated("Deprecated in Java")
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int,
        ) {
            @Suppress("DEPRECATION") val value =
                if (status == BluetoothGatt.GATT_SUCCESS) c.value else null
            Log.i(TAG, "onCharacteristicRead uuid=${c.uuid} status=$status len=${value?.size}")
            pendingRead?.complete(value)
            pendingRead = null
        }

        override fun onCharacteristicWrite(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int,
        ) {
            Log.i(TAG, "onCharacteristicWrite uuid=${c.uuid} status=$status")
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
            val accepted = @Suppress("DEPRECATION") run { gatt.readCharacteristic(stateChar) }
            Log.i(TAG, "readCharacteristic(STATE) accepted=$accepted")
            val value = withTimeoutOrNull(OP_TIMEOUT_MS) { deferred.await() }
                ?: throw IOException("read STATE timeout")
            Log.i(TAG, "read STATE value=${value?.joinToString(" ") { "%02x".format(it) }}")
            val b = value?.firstOrNull() ?: return ProvFrames.STATE_UNPROVISIONED
            return b.toInt() and 0xFF
        }

        override suspend fun writeServerKey(key: ByteArray) {
            require(key.size == ProvFrames.KEY_LEN) { "server key must be ${ProvFrames.KEY_LEN} bytes" }
            // Enlarge the MTU now (just before the 32-byte write that needs it).
            // Wait up to MTU_TIMEOUT_MS for the exchange. If it never fires (some
            // vendor stacks don't fire onMtuChanged), proceed anyway — the write
            // then fails with a clear length-status error instead of hanging.
            if (gatt.requestMtu(247)) {
                withTimeoutOrNull(MTU_TIMEOUT_MS) { cb.mtuReady.await() }
            }
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

