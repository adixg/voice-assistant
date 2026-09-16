package com.aigf.blespike

// Phase 3 spike, part 2 -- extends the byte-envelope codec (confirmed
// round-tripping on real hardware 2026-09-16) with bonding + the app-layer
// shared-secret AUTH handshake + TIME_SYNC, mirroring
// firmware/m5stick_ble_flash_spike/src/main.cpp's own header comment for
// the full security model and payload formats -- read that first, this
// side must match it exactly.
//
// New connect sequence: connect -> PHY/MTU -> discover services -> bond
// (device.createBond() if not already bonded, waiting for
// ACTION_BOND_STATE_CHANGED=BONDED) -> send AUTH, then TIME_SYNC, then a
// test HEARD frame, one at a time over the RX characteristic -> only then
// subscribe to TX notifications. Subscribing last isn't strictly required
// by the firmware (it gates on appAuthed, not subscribe order) but keeps
// the log linear and easy to read while testing.
//
// No Tailscale/WebSocket bridging here -- that's Phase 5's job, once all of
// Phase 3 is done. See
// /home/aditya/.claude/plans/tranquil-drifting-stream.md, Phases 2-3.

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
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import java.util.concurrent.atomic.AtomicLong

class MainActivity : AppCompatActivity() {

    companion object {
        // Freshly generated, not the well-known Nordic UART Service UUID this
        // started with -- see the comment in main.cpp for why that mattered.
        // Must match firmware/m5stick_ble_flash_spike/src/main.cpp exactly.
        val SERVICE_UUID: UUID = UUID.fromString("667d22e3-b922-4852-b7a6-6a4764a26665")
        val TX_CHAR_UUID: UUID = UUID.fromString("c0819f6b-f0b7-4db8-9dcb-9f58b2745f9c")
        val RX_CHAR_UUID: UUID = UUID.fromString("9e5d1e40-6b0a-4b7a-9c2e-5b7c9a6a0e11")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        // Must match main.cpp's SHARED_SECRET exactly -- spike-only placeholder,
        // same caveat as that file's own comment on it.
        const val SHARED_SECRET = "spike-shared-secret-change-me"
        const val REQUEST_PERMS = 1
        // Real logcat output (Log.d), unlike log() below which only appends to
        // the on-screen TextView -- adb logcat --pid=<p> never showed this
        // app's own messages, only the framework's, until this existed.
        const val TAG = "BleSpike"
    }

    private lateinit var logView: TextView
    private val handler = Handler(Looper.getMainLooper())
    private var gatt: BluetoothGatt? = null
    private var txCharacteristic: BluetoothGattCharacteristic? = null
    private var rxCharacteristic: BluetoothGattCharacteristic? = null

    // Set while waiting for ACTION_BOND_STATE_CHANGED=BONDED; run once and
    // cleared by bondReceiver below.
    private var pendingBondAction: (() -> Unit)? = null

    // Bonding is a device-level (Bluetooth stack) event, not a per-GATT-op
    // callback -- this is the only way to know it actually completed rather
    // than assuming createBond() succeeded just because it returned true
    // (that return value only means "request accepted", same trap as every
    // other BluetoothGatt call this file has already had to fix once).
    private val bondReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != BluetoothDevice.ACTION_BOND_STATE_CHANGED) return
            val state = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.BOND_NONE)
            val name = when (state) {
                BluetoothDevice.BOND_BONDING -> "BONDING"
                BluetoothDevice.BOND_BONDED -> "BONDED"
                BluetoothDevice.BOND_NONE -> "NONE"
                else -> "?($state)"
            }
            Log.d(TAG, "bond state -> $name")
            log("Bond state -> $name")
            if (state == BluetoothDevice.BOND_BONDED) {
                val action = pendingBondAction
                pendingBondAction = null
                action?.invoke()
            }
        }
    }

    private val bytesThisSecond = AtomicLong(0)
    private val notifiesThisSecond = AtomicLong(0)

    // Decodes TX notifications back into logical frames -- see
    // BleEnvelopeCodec.kt's own header comment for why this must stay in
    // exact lockstep with ble_envelope.h.
    private val rxDecoder = BleEnvelopeDecoder { type, payload ->
        val text = String(payload, Charsets.UTF_8)
        Log.d(TAG, "decoded frame type=0x%02X len=%d: %s".format(type, payload.size, text))
        log("RX frame 0x%02X (%d B): %s".format(type, payload.size, text))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        logView = findViewById(R.id.logView)
        log("Phase 3 spike starting")

        // RECEIVER_EXPORTED, not RECEIVER_NOT_EXPORTED -- tried NOT_EXPORTED
        // first (the generally-recommended flag for a system-only protected
        // broadcast like this one) and confirmed via `adb shell dumpsys
        // activity broadcasts` that bonding genuinely completed
        // (BOND_BONDING -> BOND_BONDED in the Bluetooth stack's own logs)
        // while this app's registered receiver showed zero delivery history
        // -- the broadcast never reached it. Whatever this device/OEM build
        // does differently, EXPORTED is what actually receives it in
        // practice; verified against the real phone, not assumed from docs.
        ContextCompat.registerReceiver(
            this, bondReceiver, IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED),
            ContextCompat.RECEIVER_EXPORTED
        )

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
            val service = g.getService(SERVICE_UUID)
            val characteristic = service?.getCharacteristic(TX_CHAR_UUID)
            if (characteristic == null) {
                log("Service/TX characteristic not found (status=$status)")
                return
            }
            txCharacteristic = characteristic
            rxCharacteristic = service.getCharacteristic(RX_CHAR_UUID)
            if (rxCharacteristic == null) {
                log("RX characteristic not found -- write path won't work")
                return
            }

            // RX is WRITE_ENC on the firmware side (see main.cpp), so the
            // first write needs bonding first, not just a connection --
            // explicit createBond() + waiting for the real broadcast, not
            // relying on the write silently triggering it, for a
            // deterministic and clearly-logged sequence.
            if (g.device.bondState == BluetoothDevice.BOND_BONDED) {
                log("Already bonded.")
                startAuthSequence(g)
            } else {
                log("Not bonded -- requesting bond (may show a system pairing prompt)...")
                pendingBondAction = { startAuthSequence(g) }
                g.device.createBond()
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

        override fun onCharacteristicWrite(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int
        ) {
            if (status != android.bluetooth.BluetoothGatt.GATT_SUCCESS) {
                log("RX write FAILED (status=$status), aborting rest of frame")
                pendingPacketQueues.remove(characteristic.uuid)
                return
            }
            // Pops and fires the next queued packet for this characteristic,
            // if sendTestHeardFrame()/writeNextPacket() left one -- this is
            // what actually advances a multi-packet frame across the BLE
            // stack's single operation queue, one confirmed write at a time.
            pendingPacketQueues.remove(characteristic.uuid)?.invoke()
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

    // Post-bonding sequence: AUTH (the shared secret, gates everything else
    // on the firmware side -- see main.cpp), TIME_SYNC (current epoch
    // seconds, 8 bytes little-endian, matching main.cpp's memcpy into an
    // int64_t on this same little-endian platform), then one test HEARD
    // frame. Each frame is only sent after the previous one's write(s) fully
    // complete; once the queue drains, subscribeToNotifications() runs --
    // see sendNextFrame().
    private fun startAuthSequence(g: BluetoothGatt) {
        log("Bonded -- sending AUTH, TIME_SYNC, then a test HEARD frame...")
        val epochSeconds = System.currentTimeMillis() / 1000
        val timeSyncPayload = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN)
            .putLong(epochSeconds).array()

        outgoingFrames.addLast(FrameType.AUTH to SHARED_SECRET.toByteArray(Charsets.UTF_8))
        outgoingFrames.addLast(FrameType.TIME_SYNC to timeSyncPayload)
        outgoingFrames.addLast(FrameType.HEARD to "test transcript from phone".toByteArray(Charsets.UTF_8))
        onAllFramesSent = { subscribeToNotifications(g) }
        sendNextFrame(g)
    }

    private val outgoingFrames = ArrayDeque<Pair<Byte, ByteArray>>()
    private var onAllFramesSent: (() -> Unit)? = null

    private fun subscribeToNotifications(g: BluetoothGatt) {
        val characteristic = txCharacteristic
        if (characteristic == null) {
            log("No TX characteristic -- can't subscribe")
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

    // Pops and sends the next queued logical frame, splitting it into
    // packets via encodeFrame() same as before. When the queue is empty,
    // runs whatever onAllFramesSent was set to (startAuthSequence sets it
    // to subscribeToNotifications) exactly once.
    private fun sendNextFrame(g: BluetoothGatt) {
        val rx = rxCharacteristic
        if (rx == null) {
            log("Can't send -- no RX characteristic")
            return
        }
        val next = outgoingFrames.removeFirstOrNull()
        if (next == null) {
            val done = onAllFramesSent
            onAllFramesSent = null
            done?.invoke()
            return
        }
        val (type, payload) = next
        log("Sending frame type=0x%02X (%d B payload)...".format(type, payload.size))
        val packets = ArrayDeque<ByteArray>()
        encodeFrame(type, payload) { packets.addLast(it); true }
        writeNextPacket(g, rx, packets) { sendNextFrame(g) }
    }

    // Writes packets to the RX characteristic in order, one write per
    // packet, each waiting for onCharacteristicWrite before the next fires
    // -- writeCharacteristic() joins the same queue as everything else on
    // this BluetoothGatt (see the connect-time comment), so firing several
    // packets back-to-back here would hit the identical silent-drop bug
    // this file already spent real effort finding once. onFrameDone runs
    // once this frame's packets are all confirmed sent.
    //
    // retriesLeft handles a real, reproducible failure: the very first
    // write right after bonding completes can synchronously fail with
    // ERROR_GATT_WRITE_NOT_ALLOWED (200), even though the Stick's own
    // serial log already showed onAuthenticationComplete(encrypted=true)
    // before this write was attempted. This is a documented Android quirk
    // (Martijn van Welie's "Making Android BLE work" series: Android's own
    // stack can lag briefly before it treats a just-completed encryption as
    // settled for a write-permission check), not a bug in this app's
    // sequencing -- confirmed by hitting it on real hardware immediately
    // after "Already bonded." in this app's own log. ERROR_GATT_WRITE_
    // REQUEST_BUSY (201) is the same kind of transient condition. Peek
    // (not pop) the packet so a retry resends the same one, not the next.
    private fun writeNextPacket(
        g: BluetoothGatt, rx: BluetoothGattCharacteristic,
        packets: ArrayDeque<ByteArray>, retriesLeft: Int = 5, onFrameDone: () -> Unit
    ) {
        val packet = packets.firstOrNull()
        if (packet == null) {
            onFrameDone()
            return
        }
        val result = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            g.writeCharacteristic(rx, packet, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)
        } else {
            @Suppress("DEPRECATION")
            rx.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            @Suppress("DEPRECATION")
            rx.value = packet
            @Suppress("DEPRECATION")
            if (g.writeCharacteristic(rx)) BluetoothStatusCodes.SUCCESS else -1
        }
        when {
            result == BluetoothStatusCodes.SUCCESS -> {
                packets.removeFirst()  // only now -- the write was actually queued
                // Chained from onCharacteristicWrite, not here -- see that callback.
                pendingPacketQueues[rx.uuid] = { writeNextPacket(g, rx, packets, onFrameDone = onFrameDone) }
            }
            (result == BluetoothStatusCodes.ERROR_GATT_WRITE_NOT_ALLOWED ||
                result == BluetoothStatusCodes.ERROR_GATT_WRITE_REQUEST_BUSY) && retriesLeft > 0 -> {
                log("write not ready yet ($result), retrying ($retriesLeft left)...")
                handler.postDelayed({ writeNextPacket(g, rx, packets, retriesLeft - 1, onFrameDone) }, 300)
            }
            else -> {
                log("writeCharacteristic() failed ($result), aborting frame")
            }
        }
    }

    // One characteristic writing at a time in practice (this spike only ever
    // has RX), but keyed by UUID rather than a bare callback so this doesn't
    // silently break if a second write characteristic gets added later.
    private val pendingPacketQueues = mutableMapOf<UUID, () -> Unit>()

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
        if (value != null) rxDecoder.feed(value)
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
        unregisterReceiver(bondReceiver)
        gatt?.close()
    }
}
