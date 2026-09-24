package com.aigf.blespike

// The BLE-central + WebSocket-gateway app, replacing
// tools/termux_relay.py's job -- but unlike that script (a byte-blind pump
// between two WebSocket endpoints, since both sides already speak
// WebSocket), this service does real protocol translation: the Stick side
// is BLE now, not WebSocket, so every frame crosses formats one way or the
// other. See ble_envelope.h's header comment for the frame-type table this
// translation is built from, and firmware/m5stick_bridge/src/main.cpp's
// webSocketEvent()/the button handlers for the exact WS message shapes on
// the gateway side ("start"/"stop"/"reset" text, raw binary mic
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
// gateway opens right after AUTH is sent; there's no AUTH ack
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
        private const val BLE_MAX_RESCAN_DELAY_MS = 15_000L
        private const val BLE_SCAN_TIMEOUT_MS = 20_000L
        private const val BLE_HANDSHAKE_TIMEOUT_MS = 20_000L
        private const val BLE_BOND_TIMEOUT_MS = 45_000L
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
    private var scanning = false
    private var bleReady = false
    private var bleRetryAttempt = 0
    private var txCharacteristic: BluetoothGattCharacteristic? = null
    private var rxCharacteristic: BluetoothGattCharacteristic? = null
    private var pendingBondAction: (() -> Unit)? = null
    private val outgoingFrames = ArrayDeque<Pair<Byte, ByteArray>>()
    private var sendingFrames = false
    private val pendingPacketQueues = mutableMapOf<UUID, () -> Unit>()
    private var scanTimeout: Runnable? = null
    private var handshakeTimeout: Runnable? = null

    private val bleReconnect = Runnable { startScan() }

    private val bondReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != BluetoothDevice.ACTION_BOND_STATE_CHANGED) return
            val state = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.BOND_NONE)
            val previousState = intent.getIntExtra(
                BluetoothDevice.EXTRA_PREVIOUS_BOND_STATE, BluetoothDevice.BOND_NONE
            )
            val device = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
            } else {
                @Suppress("DEPRECATION")
                intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
            }
            if (device?.address != gatt?.device?.address) return
            Log.d(TAG, "bond state -> $state")
            if (state == BluetoothDevice.BOND_BONDED) {
                val action = pendingBondAction
                pendingBondAction = null
                action?.invoke()
            } else if (state == BluetoothDevice.BOND_NONE && previousState == BluetoothDevice.BOND_BONDING) {
                pendingBondAction = null
                restartBle("Pairing failed")
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
    private val wsReconnect = Runnable { connectWs() }
    private val httpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    private val wsListener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (webSocket !== ws) {
                webSocket.close(1000, "stale connection")
                return
            }
            log("WS connected to gateway")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (webSocket !== ws || !bleReady) return
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
            if (webSocket !== ws || !bleReady) return
            enqueueFrame(FrameType.AUDIO_CHUNK, bytes.toByteArray())
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (webSocket !== ws) return
            log("WS closed ($code $reason)")
            scheduleWsReconnect()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (webSocket !== ws) return
            log("WS failed: ${t.message}")
            scheduleWsReconnect()
        }
    }

    private fun connectWs() {
        handler.removeCallbacks(wsReconnect)
        if (!running || !bleReady || ws != null) return
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
        handler.removeCallbacks(wsReconnect)
        if (!running || !bleReady) return
        handler.postDelayed(wsReconnect, WS_RECONNECT_DELAY_MS)
    }

    private fun disconnectWs(reason: String) {
        handler.removeCallbacks(wsReconnect)
        val oldWs = ws
        ws = null
        oldWs?.close(1000, reason)
    }

    private fun sendWsText(text: String) {
        if (ws?.send(text) != true) log("WS not connected -- dropped '$text'")
    }

    private fun sendWsBinary(bytes: ByteArray) {
        if (ws?.send(ByteString.of(*bytes)) != true) log("WS not connected -- dropped ${bytes.size}B audio chunk")
    }

    // ------------------------------------------------------ BLE <-> frames
    private fun enqueueFrame(type: Byte, payload: ByteArray) {
        if (!bleReady && type != FrameType.AUTH) return
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
        // Reply audio goes without response: an acknowledged write costs a
        // round trip per ~500-byte packet, which measured ~12 KB/s against the
        // Stick's 32 KB/s playback, so replies stuttered. Everything else (AUTH,
        // control, text) stays acknowledged. Android still calls
        // onCharacteristicWrite once a no-response packet is handed to the
        // controller, so the one-write-at-a-time queue keeps its flow control.
        val writeType = if (type == FrameType.AUDIO_CHUNK) {
            BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        } else {
            BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        }
        writeNextPacket(g, rx, packets, writeType) { sendNextFrame() }
    }

    // Same design (peek-not-pop + bounded retry on ERROR_GATT_WRITE_NOT_ALLOWED/
    // ERROR_GATT_WRITE_REQUEST_BUSY) as the Phase 3 spike -- see
    // MainActivity.kt's git history / docs/ble-migration.md for why: a real,
    // reproducible Android quirk confirmed on hardware, not a guess.
    private fun writeNextPacket(
        g: BluetoothGatt, rx: BluetoothGattCharacteristic,
        packets: ArrayDeque<ByteArray>, writeType: Int,
        retriesLeft: Int = if (writeType == BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE) 100 else 5,
        onFrameDone: () -> Unit
    ) {
        if (g !== gatt || !running) return
        val packet = packets.firstOrNull()
        if (packet == null) {
            onFrameDone()
            return
        }
        val result = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            g.writeCharacteristic(rx, packet, writeType)
        } else {
            @Suppress("DEPRECATION")
            rx.writeType = writeType
            @Suppress("DEPRECATION")
            rx.value = packet
            @Suppress("DEPRECATION")
            if (g.writeCharacteristic(rx)) BluetoothStatusCodes.SUCCESS else -1
        }
        when {
            result == BluetoothStatusCodes.SUCCESS -> {
                packets.removeFirst()
                pendingPacketQueues[rx.uuid] = { writeNextPacket(g, rx, packets, writeType, onFrameDone = onFrameDone) }
            }
            (result == BluetoothStatusCodes.ERROR_GATT_WRITE_NOT_ALLOWED ||
                result == BluetoothStatusCodes.ERROR_GATT_WRITE_REQUEST_BUSY) && retriesLeft > 0 -> {
                // A full controller buffer is routine when streaming without
                // response, so retry that quickly; acknowledged writes keep the
                // original slow backoff for the Android quirk described above.
                val backoffMs = if (writeType == BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE) 10L else 300L
                handler.postDelayed({ writeNextPacket(g, rx, packets, writeType, retriesLeft - 1, onFrameDone) }, backoffMs)
            }
            else -> restartBle("BLE write could not be queued (status=$result)")
        }
    }

    private fun sendAuthAndStartTimeSync() {
        bleReady = true
        bleRetryAttempt = 0
        cancelHandshakeTimeout()
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
            if (!running || !bleReady || gatt == null) return
            sendTimeSync()
            handler.postDelayed(this, TIME_SYNC_INTERVAL_MS)
        }
    }

    // -------------------------------------------------------------- BLE GATT
    private fun startScan() {
        handler.removeCallbacks(bleReconnect)
        if (!running || scanning || gatt != null) return
        val adapter = (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) {
            log("Bluetooth is off")
            handler.postDelayed(bleReconnect, BLE_RESCAN_DELAY_MS * 5)
            return
        }
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(SERVICE_UUID)).build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        log("Scanning for the Stick...")
        try {
            scanning = true
            adapter.bluetoothLeScanner.startScan(listOf(filter), settings, scanCallback)
            val timeout = Runnable {
                if (!scanning) return@Runnable
                stopScan()
                scheduleBleReconnect("Stick not found yet")
            }
            scanTimeout = timeout
            handler.postDelayed(timeout, BLE_SCAN_TIMEOUT_MS)
        } catch (e: Exception) {
            scanning = false
            log("Could not start BLE scan: ${e.message}")
            scheduleBleReconnect()
        }
    }

    private fun stopScan() {
        scanTimeout?.let(handler::removeCallbacks)
        scanTimeout = null
        if (!scanning) return
        scanning = false
        try {
            val adapter = (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter
            if (adapter?.isEnabled == true) adapter.bluetoothLeScanner.stopScan(scanCallback)
        } catch (_: Exception) {
            // The radio may have gone away between isEnabled and stopScan.
        }
    }

    private fun armHandshakeTimeout(g: BluetoothGatt, stage: String, timeoutMs: Long = BLE_HANDSHAKE_TIMEOUT_MS) {
        cancelHandshakeTimeout()
        val timeout = Runnable {
            if (running && g === gatt && !bleReady) restartBle("$stage timed out", g)
        }
        handshakeTimeout = timeout
        handler.postDelayed(timeout, timeoutMs)
    }

    private fun cancelHandshakeTimeout() {
        handshakeTimeout?.let(handler::removeCallbacks)
        handshakeTimeout = null
    }

    private fun scheduleBleReconnect(reason: String? = null) {
        handler.removeCallbacks(bleReconnect)
        if (!running) return
        val shift = bleRetryAttempt.coerceAtMost(4)
        val delay = (BLE_RESCAN_DELAY_MS * (1L shl shift)).coerceAtMost(BLE_MAX_RESCAN_DELAY_MS)
        bleRetryAttempt++
        if (reason != null) log("$reason; retrying in ${delay / 1000}s")
        handler.postDelayed(bleReconnect, delay)
    }

    /**
     * Fully releases the Android GATT client before retrying. A scan alone is
     * not enough: an unclosed client can leave the peripheral connected and
     * therefore no longer advertising, which used to make a Stick power cycle
     * look necessary.
     */
    private fun restartBle(
        reason: String,
        source: BluetoothGatt? = gatt,
        alreadyDisconnected: Boolean = false
    ) {
        if (source != null && source !== gatt) {
            source.close()
            return
        }
        log(reason)
        stopScan()
        cancelHandshakeTimeout()
        handler.removeCallbacks(timeSyncTick)
        pendingBondAction = null
        bleReady = false
        txCharacteristic = null
        rxCharacteristic = null
        outgoingFrames.clear()
        pendingPacketQueues.clear()
        sendingFrames = false
        disconnectWs("BLE reconnecting")

        val oldGatt = gatt
        gatt = null
        if (oldGatt != null) {
            if (!alreadyDisconnected) {
                try {
                    oldGatt.disconnect()
                } catch (_: Exception) {
                    // close() below is the important resource release.
                }
            }
            oldGatt.close()
        }
        scheduleBleReconnect()
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            handler.post {
                if (!running || !scanning || gatt != null) return@post
                val device = result.device
                stopScan()
                log("Found ${device.address}, connecting...")
                try {
                    val newGatt = device.connectGatt(
                        this@RelayService, false, gattCallback, BluetoothDevice.TRANSPORT_LE
                    )
                    gatt = newGatt
                    armHandshakeTimeout(newGatt, "BLE connection")
                } catch (e: Exception) {
                    log("Could not connect: ${e.message}")
                    scheduleBleReconnect()
                }
            }
        }

        override fun onScanFailed(errorCode: Int) {
            handler.post {
                scanning = false
                scanTimeout?.let(handler::removeCallbacks)
                scanTimeout = null
                scheduleBleReconnect("Scan failed ($errorCode)")
            }
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            handler.post {
                if (g !== gatt) {
                    g.close()
                    return@post
                }
                if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                    log("Connected. Requesting PHY 2M...")
                    armHandshakeTimeout(g, "PHY negotiation")
                    // Do not queue requestConnectionPriority here. Android has
                    // one GATT operation lane, so it can clobber the PHY/MTU
                    // sequence that follows. The Stick requests its preferred
                    // connection parameters from the peripheral side anyway.
                    g.setPreferredPhy(
                        BluetoothDevice.PHY_LE_2M_MASK, BluetoothDevice.PHY_LE_2M_MASK,
                        BluetoothDevice.PHY_OPTION_NO_PREFERRED
                    )
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    restartBle("Disconnected (status=$status); reconnecting", g, alreadyDisconnected = true)
                } else if (status != BluetoothGatt.GATT_SUCCESS) {
                    restartBle("BLE connection failed (status=$status)", g)
                }
            }
        }

        override fun onPhyUpdate(g: BluetoothGatt, txPhy: Int, rxPhy: Int, status: Int) {
            handler.post {
                if (g !== gatt) return@post
                armHandshakeTimeout(g, "MTU negotiation")
                if (!g.requestMtu(517)) restartBle("Could not request BLE MTU", g)
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            handler.post {
                if (g !== gatt) return@post
                if (status != BluetoothGatt.GATT_SUCCESS || mtu < 503) {
                    restartBle("MTU negotiation failed (status=$status, mtu=$mtu)", g)
                    return@post
                }
                log("MTU negotiated: $mtu. Discovering services...")
                armHandshakeTimeout(g, "Service discovery")
                if (!g.discoverServices()) restartBle("Could not start service discovery", g)
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            handler.post {
                if (g !== gatt) return@post
                val service = if (status == BluetoothGatt.GATT_SUCCESS) g.getService(SERVICE_UUID) else null
                val tx = service?.getCharacteristic(TX_CHAR_UUID)
                val rx = service?.getCharacteristic(RX_CHAR_UUID)
                if (tx == null || rx == null) {
                    restartBle("Service/characteristics not found (status=$status)", g)
                    return@post
                }
                txCharacteristic = tx
                rxCharacteristic = rx

                if (g.device.bondState == BluetoothDevice.BOND_BONDED) {
                    subscribeToNotifications(g, tx)
                } else {
                    log("Not bonded -- requesting bond...")
                    armHandshakeTimeout(g, "Pairing", BLE_BOND_TIMEOUT_MS)
                    pendingBondAction = { subscribeToNotifications(g, tx) }
                    if (!g.device.createBond() && g.device.bondState != BluetoothDevice.BOND_BONDED) {
                        pendingBondAction = null
                        restartBle("Could not start pairing", g)
                    }
                }
            }
        }

        private fun subscribeToNotifications(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            if (g !== gatt) return
            armHandshakeTimeout(g, "Notification subscription")
            if (!g.setCharacteristicNotification(characteristic, true)) {
                restartBle("Could not enable local notifications", g)
                return
            }
            val cccd = characteristic.getDescriptor(CCCD_UUID) ?: run {
                restartBle("CCCD descriptor missing", g)
                return
            }
            val value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            val queued = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeDescriptor(cccd, value)
            } else {
                @Suppress("DEPRECATION")
                cccd.value = value
                @Suppress("DEPRECATION")
                if (g.writeDescriptor(cccd)) BluetoothStatusCodes.SUCCESS else -1
            }
            if (queued != BluetoothStatusCodes.SUCCESS) {
                restartBle("Could not subscribe (status=$queued)", g)
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            handler.post {
                if (g !== gatt || descriptor.uuid != CCCD_UUID) return@post
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    log("Subscribed. Sending AUTH...")
                    sendAuthAndStartTimeSync()
                } else {
                    restartBle("CCCD write failed (status=$status)", g)
                }
            }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            handler.post {
                if (g !== gatt) return@post
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    pendingPacketQueues.remove(characteristic.uuid)
                    restartBle("BLE write failed (status=$status)", g)
                    return@post
                }
                pendingPacketQueues.remove(characteristic.uuid)?.invoke()
            }
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            val value = characteristic.value.copyOf()
            handler.post { if (g === gatt && bleReady) rxDecoder.feed(value) }
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray
        ) {
            val copy = value.copyOf()
            handler.post { if (g === gatt && bleReady) rxDecoder.feed(copy) }
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
        shutdownRelay()
        unregisterReceiver(bondReceiver)
        httpClient.dispatcher.executorService.shutdown()
        httpClient.connectionPool.evictAll()
        super.onDestroy()
    }

    /**
     * Synchronously release both transports. stopSelf() is asynchronous, so
     * doing this only in onDestroy left a small window where a quick Start
     * could revive the old service with its stale GATT client still attached.
     */
    private fun shutdownRelay() {
        running = false
        stopScan()
        handler.removeCallbacksAndMessages(null)
        disconnectWs("service stopping")
        pendingBondAction = null
        bleReady = false
        txCharacteristic = null
        rxCharacteristic = null
        outgoingFrames.clear()
        pendingPacketQueues.clear()
        sendingFrames = false
        val oldGatt = gatt
        gatt = null
        if (oldGatt != null) {
            // disconnect() makes the Stick advertise again immediately;
            // close() alone can leave a controller-level zombie connection
            // until Android eventually notices it has no GATT client.
            try {
                oldGatt.disconnect()
            } catch (_: Exception) {
                // Still release the client below.
            }
            oldGatt.close()
        }
    }

    fun stopRelay() {
        shutdownRelay()
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
