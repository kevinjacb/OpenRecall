package com.sense.relay.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.ParcelUuid
import android.util.Log
import java.util.ArrayDeque
import java.util.UUID

/**
 * BLE central: finds the "Sense" device, subscribes to its audio + ack notifications,
 * and writes signed §D commands. A thin transport around the relay brain.
 *
 * Android GATT allows only ONE outstanding operation at a time, so all writes (CCCD
 * subscribes, command writes) go through a serial [opQueue] drained on each callback.
 * NOTE: written to the contract but NOT validated on-device — GATT timing/quirks vary
 * by phone; verify subscribe order and MTU negotiation on real hardware.
 */
@SuppressLint("MissingPermission")
class SensorLink(private val context: Context, private val listener: Listener) {

    interface Listener {
        fun onConnected()
        fun onAudio(packet: ByteArray)
        fun onCommandAck(payload: ByteArray)
        fun onDisconnected(reason: String)
    }

    companion object {
        private const val TAG = "SensorLink"
        val SERVICE: UUID = UUID.fromString("6e9d0001-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val AUDIO: UUID = UUID.fromString("6e9d0002-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val COMMAND: UUID = UUID.fromString("6e9d0003-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        val ACK: UUID = UUID.fromString("6e9d0004-b5a3-4f6e-9b1a-7c2d5e8f0a10")
        private val CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }

    private val adapter =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
    private var gatt: BluetoothGatt? = null
    private var commandChar: BluetoothGattCharacteristic? = null
    private var deviceAddress: String? = null

    // Serial GATT op queue: each op runs, its callback dequeues + runs the next.
    private val opQueue = ArrayDeque<() -> Unit>()
    private var opInFlight = false

    /**
     * The BLE address of the connected device, or null until the
     * scan callback sees one. Exposed so [com.sense.relay.RelayService]
     * can publish it to [com.sense.relay.relay.RelayController] on
     * every state transition.
     */
    fun deviceAddress(): String? = deviceAddress

    fun start() {
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(SERVICE)).build()
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        adapter.bluetoothLeScanner.startScan(listOf(filter), settings, scanCallback)
        Log.i(TAG, "scanning for Sense…")
    }

    fun stop() {
        runCatching { adapter.bluetoothLeScanner.stopScan(scanCallback) }
        gatt?.disconnect()
        gatt?.close()
        gatt = null
    }

    /** Write a signed command frame ([raw 64-byte sig][payload JSON]) to the device. */
    fun writeCommand(frame: ByteArray) {
        val ch = commandChar ?: return
        enqueue {
            ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            @Suppress("DEPRECATION") run { ch.value = frame; gatt?.writeCharacteristic(ch) }
        }
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            adapter.bluetoothLeScanner.stopScan(this)
            Log.i(TAG, "found ${result.device.address}, connecting")
            // Capture the address for [RelayController] to surface
            // to the UI. The scan result is the only point at which
            // we know it; the gatt callback hands us the same device
            // object but we save the indirection.
            deviceAddress = result.device.address
            // Transport must be BluetoothDevice.TRANSPORT_* (LE for a single-mode
            // BLE device). BluetoothProfile.GATT is a profile id, not a transport;
            // passing it causes the link-layer connect to be silently dropped on
            // many Android 10 vendor stacks.
            gatt = result.device.connectGatt(
                context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            // Log status + newState so a stack-rejected connect (status != 0) is
            // diagnosable from logcat.
            Log.i(TAG, "onConnectionStateChange status=$status newState=$newState")
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                g.requestMtu(247)  // DLE-friendly; larger §C.6 chunks per notification
            } else {
                listener.onDisconnected("gatt state $newState (status=$status)")
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.i(TAG, "MTU=$mtu; discovering services")
            g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val svc = g.getService(SERVICE) ?: run {
                listener.onDisconnected("Sense service not found"); return
            }
            commandChar = svc.getCharacteristic(COMMAND)
            // Subscribe to audio then ack (queued; order matters for setup).
            subscribe(g, svc.getCharacteristic(AUDIO))
            subscribe(g, svc.getCharacteristic(ACK))
            enqueue { listener.onConnected() }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) =
            nextOp()

        override fun onCharacteristicWrite(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int,
        ) = nextOp()

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            @Suppress("DEPRECATION") val value = c.value ?: return
            when (c.uuid) {
                AUDIO -> listener.onAudio(value)
                ACK -> listener.onCommandAck(value)
            }
        }
    }

    private fun subscribe(g: BluetoothGatt, ch: BluetoothGattCharacteristic?) {
        ch ?: return
        enqueue {
            g.setCharacteristicNotification(ch, true)
            val cccd = ch.getDescriptor(CCCD)
            @Suppress("DEPRECATION") run {
                cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                g.writeDescriptor(cccd)
            }
        }
    }

    private fun enqueue(op: () -> Unit) {
        synchronized(opQueue) {
            opQueue.add(op)
            if (!opInFlight) drain()
        }
    }

    private fun nextOp() {
        synchronized(opQueue) { opInFlight = false; drain() }
    }

    private fun drain() {
        val op = opQueue.poll() ?: return
        opInFlight = true
        op()
    }
}
