package com.aigf.blespike

// Phase 5: the real BLE-central + WebSocket-bridge app, replacing
// tools/termux_relay.py's job -- but unlike that script (a byte-blind pump
// between two WebSocket endpoints, since both sides already speak
// WebSocket), this service does real protocol translation: the Stick side
// is BLE now, not WebSocket, so every frame crosses formats one way or the
// other. See ble_envelope.h's header comment for the frame-type table this
// translation is built from, and firmware/m5stick_bridge/src/main.cpp's
// webSocketEvent()/the button handlers for the exact WS message shapes on
// the bridge_server.py side ("start"/"stop"/"reset" text, raw binary mic
// chunks, "heard:"/"status:"/"reply:" text, binary reply audio, "end" text).
//
// Runs as a foreground service (not tied to MainActivity's lifecycle) so
// the BLE+WebSocket bridge survives the user leaving the app -- this is the
// piece Phase 2's spike deliberately didn't need, since a throughput test
// only had to run while someone was watching the screen.
//
// Connect sequence (BLE side unchanged from the Phase 3 spike, proven on
// real hardware 2026-09-16): scan -> connect -> PHY/MTU -> discover
// services -> bond if needed -> subscribe to TX -> enqueue AUTH, then
// TIME_SYNC (re-sent periodically after -- NTP goes away without Wi-Fi, so
// this is the Stick's only wall-clock source over BLE). The WebSocket to
// bridge_server.py opens right after AUTH is sent; there's no AUTH ack
// frame in this protocol, so "sent" is as close to "ready" as this side can
// tell without adding one.

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Binder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.ParcelUuid
import android.util.Log
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

class RelayService : Service() {

    companion object {
        val SERVICE_UUID: UUID = UUID.fromString("667d22e3-b922-4852-b7a6-6a4764a26665")
        val TX_CHAR_UUID: UUID = UUID.fromString("c0819f6b-f0b7-4db8-9dcb-9f58b2745f9c")
        val RX_CHAR_UUID: UUID = UUID.fromString("9e5d1e40-6b0a-4b7a-9c2e-5b7c9a6a0e11")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        const val TAG = "RelayService"

        private const val NOTIF_CHANNEL = "relay"
        private const val NOTIF_ID = 1
        private const val TIME_SYNC_INTERVAL_MS = 5 * 60_000L
        private const val WS_RECONNECT_DELAY_MS = 3_000L
        private const val BLE_RESCAN_DELAY_MS = 1_000L
    }

    inner class LocalBinder : Binder() {
        fun service(): RelayService = this@RelayService
    }

    private val binder = LocalBinder()
    private val handler = Handler(Looper.getMainLooper())
    var logListener: ((String) -> Unit)? = null

    private lateinit var prefs: Prefs
    private var running = false

    // -------------------------------------------------------------- BLE
    private var gatt: BluetoothGatt? = null
    private var txCharacteristic: BluetoothGattCharacteristic? = null
    private var rxCharacteristic: BluetoothGattCharacteristic? = null
    private var pendingBondAction: (() -> Unit)? = null
    private val outgoingFrames = ArrayDeque<Pair<Byte, ByteArray>>()
    private var sendingFrames = false
    private val pendingPacketQueues = mutableMapOf<UUID, () -> Unit>()

    private val bondReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != BluetoothDevice.ACTION_BOND_STATE_CHANGED) return
            val state = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.BOND_NONE)
            Log.d(TAG, "bond state -> $state")
            if (state == BluetoothDevice.BOND_BONDED) {
                val action = pendingBondAction
                pendingBondAction = null
                action?.invoke()
            }
        }
    }

    private val rxDecoder = BleEnvelopeDecoder { type, payload ->
        // Frames the Stick sends over TX -- only what main.cpp's header
        // comment says TX carries. Anything else showing up here means the
        // two sides' protocols have drifted; log it rather than silently
        // dropping so that's visible.
        when (type) {
            FrameType.START -> { log("start"); sendWsText("start") }
            FrameType.STOP -> { log("stop"); sendWsText("stop") }
            FrameType.RESET -> { log("reset"); sendWsText("reset") }
            FrameType.AUDIO_CHUNK -> sendWsBinary(payload)
            else -> log("unexpected TX frame type=0x%02X len=%d".format(type, payload.size))
        }
    }

    // -------------------------------------------------------------- WS
    private var ws: WebSocket? = null
    private val httpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    private val wsListener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            log("WS connected to bridge_server.py")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            when {
                text.startsWith("heard:") ->
                    enqueueFrame(FrameType.HEARD, text.substring(6).toByteArray(Charsets.UTF_8))
                text.startsWith("status:") ->
                    enqueueFrame(FrameType.STATUS, text.substring(7).toByteArray(Charsets.UTF_8))
                text.startsWith("reply:") ->
                    enqueueFrame(FrameType.REPLY, text.substring(6).toByteArray(Charsets.UTF_8))
                text == "end" -> enqueueFrame(FrameType.END, ByteArray(0))
                else -> log("unrecognized WS text: $text")
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            enqueueFrame(FrameType.AUDIO_CHUNK, bytes.toByteArray())
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            log("WS closed ($code $reason)")
            scheduleWsReconnect()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            log("WS failed: ${t.message}")
            scheduleWsReconnect()
        }
    }

    private fun connectWs() {
        val host = prefs.bridgeHost
        if (host.isBlank()) {
            log("No bridge host configured -- open the app and set one")
            return
        }
        val url = "ws://$host:${prefs.bridgePort}/"
        log("Dialing $url ...")
        ws = httpClient.newWebSocket(Request.Builder().url(url).build(), wsListener)
    }

    private fun scheduleWsReconnect() {
        ws = null
        if (!running) return
        handler.postDelayed({ if (running) connectWs() }, WS_RECONNECT_DELAY_MS)
    }

    private fun sendWsText(text: String) {
        if (ws?.send(text) != true) log("WS not connected -- dropped '$text'")
    }

    private fun sendWsBinary(bytes: ByteArray) {
        if (ws?.send(ByteString.of(*bytes)) != true) log("WS not connected -- dropped ${bytes.size}B audio chunk")
    }

    // ------------------------------------------------------ BLE <-> frames
    private fun enqueueFrame(type: Byte, payload: ByteArray) {
        outgoingFrames.addLast(type to payload)
        if (!sendingFrames) {
            sendingFrames = true
            sendNextFrame()
        }
    }

    private fun sendNextFrame() {
        val g = gatt
        val rx = rxCharacteristic
        val next = outgoingFrames.removeFirstOrNull()
        if (g == null || rx == null || next == null) {
            sendingFrames = false
            return
        }
        val (type, payload) = next
        val packets = ArrayDeque<ByteArray>()
        encodeFrame(type, payload) { packets.addLast(it); true }
        writeNextPacket(g, rx, packets) { sendNextFrame() }
    }

    // Same design (peek-not-pop + bounded retry on ERROR_GATT_WRITE_NOT_ALLOWED/
    // ERROR_GATT_WRITE_REQUEST_BUSY) as the Phase 3 spike -- see
    // MainActivity.kt's git history / docs/ble-migration.md for why: a real,
    // reproducible Android quirk confirmed on hardware, not a guess.
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
                packets.removeFirst()
                pendingPacketQueues[rx.uuid] = { writeNextPacket(g, rx, packets, onFrameDone = onFrameDone) }
            }
            (result == BluetoothStatusCodes.ERROR_GATT_WRITE_NOT_ALLOWED ||
                result == BluetoothStatusCodes.ERROR_GATT_WRITE_REQUEST_BUSY) && retriesLeft > 0 -> {
                handler.postDelayed({ writeNextPacket(g, rx, packets, retriesLeft - 1, onFrameDone) }, 300)
            }
            else -> log("writeCharacteristic() failed ($result), dropped a frame")
        }
    }

    private fun sendAuthAndStartTimeSync() {
        enqueueFrame(FrameType.AUTH, prefs.sharedSecret.toByteArray(Charsets.UTF_8))
        sendTimeSync()
        connectWs()
        timeSyncTick.run()
    }

    private fun sendTimeSync() {
        val epochSeconds = System.currentTimeMillis() / 1000
        val payload = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putLong(epochSeconds).array()
        enqueueFrame(FrameType.TIME_SYNC, payload)
    }

    private val timeSyncTick = object : Runnable {
        override fun run() {
            if (!running || gatt == null) return
            sendTimeSync()
            handler.postDelayed(this, TIME_SYNC_INTERVAL_MS)
        }
    }

    // -------------------------------------------------------------- BLE GATT
    private fun startScan() {
        if (!running) return
        val adapter = (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) {
            log("Bluetooth is off")
            handler.postDelayed({ startScan() }, BLE_RESCAN_DELAY_MS * 5)
            return
        }
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(SERVICE_UUID)).build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        log("Scanning for the Stick...")
        adapter.bluetoothLeScanner.startScan(listOf(filter), settings, scanCallback)
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val device = result.device
            log("Found ${device.address}, connecting...")
            (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter.bluetoothLeScanner.stopScan(this)
            gatt = device.connectGatt(this@RelayService, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        }

        override fun onScanFailed(errorCode: Int) {
            log("Scan failed: $errorCode")
            handler.postDelayed({ startScan() }, BLE_RESCAN_DELAY_MS)
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                log("Connected. Requesting PHY 2M...")
                g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)
                g.setPreferredPhy(
                    BluetoothDevice.PHY_LE_2M_MASK, BluetoothDevice.PHY_LE_2M_MASK,
                    BluetoothDevice.PHY_OPTION_NO_PREFERRED
                )
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                log("Disconnected (status=$status)")
                gatt = null
                txCharacteristic = null
                rxCharacteristic = null
                outgoingFrames.clear()
                sendingFrames = false
                ws?.close(1000, "BLE disconnected")
                ws = null
                if (running) handler.postDelayed({ startScan() }, BLE_RESCAN_DELAY_MS)
            }
        }

        override fun onPhyUpdate(g: BluetoothGatt, txPhy: Int, rxPhy: Int, status: Int) {
            g.requestMtu(517)
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            log("MTU negotiated: $mtu. Discovering services...")
            g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val service = g.getService(SERVICE_UUID)
            val tx = service?.getCharacteristic(TX_CHAR_UUID)
            val rx = service?.getCharacteristic(RX_CHAR_UUID)
            if (tx == null || rx == null) {
                log("Service/characteristics not found (status=$status)")
                return
            }
            txCharacteristic = tx
            rxCharacteristic = rx

            if (g.device.bondState == BluetoothDevice.BOND_BONDED) {
                subscribeToNotifications(g, tx)
            } else {
                log("Not bonded -- requesting bond...")
                pendingBondAction = { subscribeToNotifications(g, tx) }
                g.device.createBond()
            }
        }

        private fun subscribeToNotifications(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            g.setCharacteristicNotification(characteristic, true)
            val cccd = characteristic.getDescriptor(CCCD_UUID) ?: run {
                log("CCCD descriptor missing -- cannot subscribe")
                return
            }
            val value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeDescriptor(cccd, value)
            } else {
                @Suppress("DEPRECATION")
                cccd.value = value
                @Suppress("DEPRECATION")
                g.writeDescriptor(cccd)
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS) {
                log("Subscribed. Sending AUTH...")
                sendAuthAndStartTimeSync()
            } else {
                log("CCCD write failed (status=$status)")
            }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                pendingPacketQueues.remove(characteristic.uuid)
                return
            }
            pendingPacketQueues.remove(characteristic.uuid)?.invoke()
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            rxDecoder.feed(characteristic.value)
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray
        ) {
            rxDecoder.feed(value)
        }
    }

    // -------------------------------------------------------------- Service
    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        ContextCompat.registerReceiver(
            this, bondReceiver, IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED),
            ContextCompat.RECEIVER_EXPORTED  // see MainActivity's earlier comment -- verified against real hardware
        )
        startForeground(NOTIF_ID, buildNotification("Starting..."), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!running) {
            running = true
            if (hasBlePermissions()) startScan() else log("Missing BLUETOOTH_SCAN/CONNECT permission")
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onDestroy() {
        running = false
        handler.removeCallbacksAndMessages(null)
        unregisterReceiver(bondReceiver)
        ws?.close(1000, "service stopping")
        gatt?.close()
        super.onDestroy()
    }

    fun stopRelay() {
        stopSelf()
    }

    private fun hasBlePermissions(): Boolean =
        ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED &&
            ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

    private fun log(line: String) {
        Log.d(TAG, line)
        handler.post {
            logListener?.invoke(line)
            updateNotification(line)
        }
    }

    private fun buildNotification(text: String): Notification {
        val mgr = getSystemService(NotificationManager::class.java)
        if (mgr.getNotificationChannel(NOTIF_CHANNEL) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(NOTIF_CHANNEL, "BLE relay", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, NOTIF_CHANNEL)
            .setContentTitle("aicompanion BLE relay")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentIntent(openApp)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.notify(NOTIF_ID, buildNotification(text))
    }
}
