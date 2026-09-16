package com.aigf.blespike

// Phase 2 throughput spike -- the BLE-central counterpart to
// firmware/m5stick_ble_flash_spike/src/main.cpp. Scans for that firmware's
// service UUID, connects, negotiates 2M PHY + max MTU, subscribes to its
// notify characteristic, and shows sustained kbps on screen every second.
// No Tailscale/WebSocket bridging here -- that's Phase 5's job, once this
// and Phase 3's protocol design are both validated. See
// /home/aditya/.claude/plans/tranquil-drifting-stream.md, Phase 2.

import android.Manifest
import android.app.Activity
import android.bluetooth.BluetoothAdapter
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
import android.bluetooth.BluetoothStatusCodes
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import java.util.UUID
import java.util.concurrent.atomic.AtomicLong

class MainActivity : AppCompatActivity() {

    companion object {
        // Freshly generated, not the well-known Nordic UART Service UUID this
        // started with -- see the comment in main.cpp for why that mattered.
        // Must match firmware/m5stick_ble_flash_spike/src/main.cpp exactly.
        val SERVICE_UUID: UUID = UUID.fromString("667d22e3-b922-4852-b7a6-6a4764a26665")
        val CHAR_UUID: UUID = UUID.fromString("c0819f6b-f0b7-4db8-9dcb-9f58b2745f9c")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        const val REQUEST_PERMS = 1
        // Real logcat output (Log.d), unlike log() below which only appends to
        // the on-screen TextView -- adb logcat --pid=<p> never showed this
        // app's own messages, only the framework's, until this existed.
        const val TAG = "BleSpike"
    }

    private lateinit var logView: TextView
    private val handler = Handler(Looper.getMainLooper())
    private var gatt: BluetoothGatt? = null

    private val bytesThisSecond = AtomicLong(0)
    private val notifiesThisSecond = AtomicLong(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        logView = findViewById(R.id.logView)
        log("Phase 2 throughput spike starting")

        val needed = listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            .filter { ActivityCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQUEST_PERMS)
        } else {
            startScan()
        }

        // Ticks the on-screen throughput readout every second, mirroring the
        // firmware's own serial report -- the two numbers should roughly
        // agree, since both are measuring the same notify stream.
        handler.post(reportTick)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_PERMS) {
            if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
                startScan()
            } else {
                log("Bluetooth permissions denied -- cannot scan.")
            }
        }
    }

    private fun startScan() {
        val adapter = (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) {
            log("Bluetooth is off -- turn it on and relaunch.")
            return
        }
        val scanner = adapter.bluetoothLeScanner
        val filter = ScanFilter.Builder().setServiceUuid(android.os.ParcelUuid(SERVICE_UUID)).build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        log("Scanning for service $SERVICE_UUID ...")
        scanner.startScan(listOf(filter), settings, scanCallback)
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val device: BluetoothDevice = result.device
            log("Found ${device.address}, connecting...")
            (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter
                .bluetoothLeScanner.stopScan(this)
            gatt = device.connectGatt(this@MainActivity, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        }

        override fun onScanFailed(errorCode: Int) {
            log("Scan failed: $errorCode")
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                log("Connected. Requesting PHY 2M...")
                // Android's connection-priority abstraction is the central-side
                // analog of the firmware's updateConnParams() -- the peripheral
                // can only *request* an interval via L2CAP; the central (this
                // phone) has final say on what's actually used on the link, so
                // this has to be requested from here too, not just the firmware.
                // Fire-and-forget: it isn't a queued ATT operation and has no
                // completion callback on this API surface.
                g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)
                // setPreferredPhy/requestMtu/discoverServices/writeDescriptor all
                // share BluetoothGatt's single internal operation queue -- firing
                // more than one before the previous op's callback lands leaves the
                // client in a broken state where later calls are silently dropped
                // (no exception, no callback, nothing). That was this app's actual
                // bug: PHY/MTU/subscribe were all fired back-to-back here, so only
                // the first ever really completed -- the firmware kept notifying a
                // peer that had a dead receive path. Chain each step off the
                // previous one's real completion callback instead.
                g.setPreferredPhy(
                    BluetoothDevice.PHY_LE_2M_MASK, BluetoothDevice.PHY_LE_2M_MASK,
                    BluetoothDevice.PHY_OPTION_NO_PREFERRED
                )
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                log("Disconnected (status=$status). Re-scanning...")
                gatt = null
                startScan()
            }
        }

        override fun onPhyUpdate(g: BluetoothGatt, txPhy: Int, rxPhy: Int, status: Int) {
            log("PHY updated: tx=${phyName(txPhy)} rx=${phyName(rxPhy)} status=$status. Requesting MTU 517...")
            g.requestMtu(517)
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            log("MTU negotiated: $mtu (status=$status). Discovering services...")
            g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val characteristic = g.getService(SERVICE_UUID)?.getCharacteristic(CHAR_UUID)
            if (characteristic == null) {
                log("Service/characteristic not found (status=$status)")
                return
            }
            g.setCharacteristicNotification(characteristic, true)
            val cccd = characteristic.getDescriptor(CCCD_UUID)
            if (cccd != null) {
                // The old descriptor.value=...; writeDescriptor(descriptor) pair is
                // deprecated since API 33 for exactly the failure mode this app hit:
                // Google's own docs call it "not memory safe... relies on a
                // BluetoothGattDescriptor object whose underlying fields are subject
                // to change outside this method." The old writeDescriptor(descriptor)
                // also returns a plain Boolean with no visibility into *why* it
                // failed -- and the return value was never even checked here, so a
                // false return (operation never queued) looked identical to a
                // pending write: onDescriptorWrite legitimately never fires for a
                // call that was rejected at the entry point, which is exactly the
                // silent hang this app was stuck in (log stopped right after
                // "Writing CCCD to subscribe...", no success, no failure, forever).
                // The phone here is SDK 36, well past where this applies.
                val value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    val result = g.writeDescriptor(cccd, value)
                    log("writeDescriptor() -> $result (SUCCESS=${BluetoothStatusCodes.SUCCESS}). Waiting for onDescriptorWrite...")
                } else {
                    @Suppress("DEPRECATION")
                    cccd.value = value
                    @Suppress("DEPRECATION")
                    val queued = g.writeDescriptor(cccd)
                    log("writeDescriptor() queued=$queued. Waiting for onDescriptorWrite...")
                }
            } else {
                log("CCCD descriptor missing -- cannot subscribe")
            }
        }

        // The only real confirmation that the CCCD write reached the peer --
        // the earlier code logged "Subscribed" right after calling
        // writeDescriptor() without ever checking this, so a silently
        // dropped/failed write (status != GATT_SUCCESS) looked identical to
        // success in the log.
        override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            if (status == android.bluetooth.BluetoothGatt.GATT_SUCCESS) {
                log("Subscribed (status=$status). Measuring throughput...")
            } else {
                log("CCCD write FAILED (status=$status) -- not subscribed, no notifications will arrive")
            }
        }

        // Pre-API-33 callback. Kept as the primary path since it works
        // uniformly across every API level this app might run on.
        override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            Log.d(TAG, "onCharacteristicChanged(2-arg) fired")
            onNotification(characteristic.value)
        }

        // API 33+ callback (value passed directly rather than read back off
        // the characteristic). Overridden too so nothing is missed on newer
        // platforms if the framework prefers this signature -- our test
        // phone is SDK 36, so this is the one actually expected to fire.
        override fun onCharacteristicChanged(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray
        ) {
            Log.d(TAG, "onCharacteristicChanged(3-arg) fired, ${value.size} bytes")
            onNotification(value)
        }
    }

    // Real Log.d, not just the on-screen log() -- this is the only way to tell
    // whether the framework ever calls back into this app at all versus the
    // app being called but failing silently somewhere after. A previous
    // logcat capture, filtered to this app's PID, showed the full GATT
    // handshake (connect/PHY/MTU/discover/subscribe) but nothing at all after
    // subscribing -- which is NOT proof notifications never arrived, since the
    // framework doesn't log onCharacteristicChanged by default either. This
    // makes that ambiguity resolvable.
    private fun onNotification(value: ByteArray?) {
        val n = value?.size ?: 0
        Log.d(TAG, "onNotification: $n bytes")
        bytesThisSecond.addAndGet(n.toLong())
        notifiesThisSecond.incrementAndGet()
    }

    private fun phyName(phy: Int): String = when (phy) {
        BluetoothDevice.PHY_LE_1M -> "1M"
        BluetoothDevice.PHY_LE_2M -> "2M"
        BluetoothDevice.PHY_LE_CODED -> "CODED"
        else -> "?($phy)"
    }

    private val reportTick = object : Runnable {
        override fun run() {
            val bytes = bytesThisSecond.getAndSet(0)
            val notifies = notifiesThisSecond.getAndSet(0)
            val kbps = bytes * 8 / 1000.0
            log(String.format("%.1f kbps  (%d notifies, %d bytes)", kbps, notifies, bytes))
            handler.postDelayed(this, 1000)
        }
    }

    private fun log(line: String) {
        handler.post {
            logView.append("\n$line")
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        handler.removeCallbacksAndMessages(null)
        gatt?.close()
    }
}
