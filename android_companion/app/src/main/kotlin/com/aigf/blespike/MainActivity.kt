package com.aigf.blespike

// Thin settings/control UI over RelayService, which owns the
// actual BLE-central + WebSocket-bridge connection so it survives this
// Activity being backgrounded or destroyed. This file used to own that
// connection directly (the Phase 2/3 spikes) -- see RelayService.kt for
// where that logic moved to and why.

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

class MainActivity : AppCompatActivity() {

    companion object {
        const val REQUEST_PERMS = 1
    }

    private lateinit var prefs: Prefs
    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var hostInput: EditText
    private lateinit var portInput: EditText
    private lateinit var secretInput: EditText
    private lateinit var startStopButton: Button
    private lateinit var firmwareText: TextView
    private lateinit var volumeLabel: TextView
    private lateinit var brightnessLabel: TextView
    private lateinit var volumeBar: SeekBar
    private lateinit var brightnessBar: SeekBar
    private lateinit var otaProgress: ProgressBar

    // Picks a firmware.bin (built with `pio run` on the machine that has
    // secrets.h, then copied to the phone) and flashes it over BLE.
    private val pickFirmware = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri == null) return@registerForActivityResult
        val bytes = try {
            contentResolver.openInputStream(uri)?.use { it.readBytes() }
        } catch (e: Exception) {
            log("Could not read the file: ${e.message}")
            null
        } ?: return@registerForActivityResult
        val service = relay
        if (service == null) {
            log("Start the relay and connect to the Stick first")
            return@registerForActivityResult
        }
        AlertDialog.Builder(this)
            .setTitle("Update the Stick?")
            .setMessage(
                "Flash ${bytes.size / 1024} KB to the Stick over Bluetooth. It takes about a minute; " +
                    "keep the phone near the Stick. If it fails, the Stick keeps its current firmware.\n\n" +
                    "The image must be built with the same shared secret as this app, or the Stick " +
                    "rolls back to its current firmware after 10 minutes without a connection."
            )
            .setPositiveButton("Update") { _, _ ->
                val why = service.startOta(bytes)
                if (why != null) log("Can't update: $why")
            }
            .setNegativeButton("Cancel", null)
            .show()
    }
    private val handler = Handler(Looper.getMainLooper())

    private var relay: RelayService? = null
    private var running = false

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            relay = (binder as RelayService.LocalBinder).service()
            relay?.logListener = { line -> log(line) }
            relay?.settingsListener = { info -> showStickInfo(info) }
            relay?.otaListener = { message, percent -> showOta(message, percent) }
            relay?.stickInfo?.let { showStickInfo(it) }
            running = true
            updateButtons()
        }

        override fun onServiceDisconnected(name: ComponentName) {
            relay = null
            running = false
            updateButtons()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = Prefs(this)

        // targetSdk 36 (Android 15+) enforces edge-to-edge unconditionally
        // -- confirmed on the real phone: the host EditText rendered half
        // behind the status bar, and WindowCompat.setDecorFitsSystemWindows
        // (window, true) had zero effect, which matches Android 15+ making
        // that call a no-op for apps targeting SDK 35+ (there's no opting
        // out anymore). The actual fix is applying the system bar insets as
        // padding on the root view directly.
        val root = findViewById<View>(R.id.rootLayout)
        val rootPaddingLeft = root.paddingLeft
        val rootPaddingTop = root.paddingTop
        val rootPaddingRight = root.paddingRight
        val rootPaddingBottom = root.paddingBottom
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(
                rootPaddingLeft + bars.left, rootPaddingTop + bars.top,
                rootPaddingRight + bars.right, rootPaddingBottom + bars.bottom
            )
            insets
        }

        logView = findViewById(R.id.logView)
        logScroll = findViewById(R.id.logScroll)
        hostInput = findViewById(R.id.hostInput)
        portInput = findViewById(R.id.portInput)
        secretInput = findViewById(R.id.secretInput)
        startStopButton = findViewById(R.id.startStopButton)
        firmwareText = findViewById(R.id.firmwareText)
        volumeLabel = findViewById(R.id.volumeLabel)
        brightnessLabel = findViewById(R.id.brightnessLabel)
        volumeBar = findViewById(R.id.volumeBar)
        brightnessBar = findViewById(R.id.brightnessBar)
        otaProgress = findViewById(R.id.otaProgress)
        volumeBar.isEnabled = false
        brightnessBar.isEnabled = false

        val settingsChanged = object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(bar: SeekBar, value: Int, fromUser: Boolean) = updateSettingLabels()
            override fun onStartTrackingTouch(bar: SeekBar) {}
            // Sent on release, not on every step: each one is a BLE write.
            override fun onStopTrackingTouch(bar: SeekBar) {
                if (relay?.sendSettings(volumeBar.progress, brightnessBar.progress) != true) {
                    log("Stick not connected -- setting not sent")
                }
            }
        }
        volumeBar.setOnSeekBarChangeListener(settingsChanged)
        brightnessBar.setOnSeekBarChangeListener(settingsChanged)
        findViewById<Button>(R.id.otaButton).setOnClickListener {
            if (relay?.otaActive == true) log("An update is already running") else pickFirmware.launch(arrayOf("*/*"))
        }

        hostInput.setText(prefs.bridgeHost)
        portInput.setText(prefs.bridgePort.toString())
        secretInput.setText(prefs.sharedSecret)

        startStopButton.setOnClickListener {
            if (running) stopRelay() else startRelay()
        }
        findViewById<Button>(R.id.forgetButton).setOnClickListener {
            // No reliable, non-hidden-API way to unpair a specific device
            // programmatically -- BluetoothDevice.removeBond() isn't public
            // SDK. Hitting the exact device from here to auto-select it
            // isn't possible either, so this opens system Bluetooth
            // settings and lets the person forget it in two taps, same as
            // the manual step docs/ble-migration.md already documents
            // needing once, after repeated reflashing left a stale bond.
            startActivity(Intent(Settings.ACTION_BLUETOOTH_SETTINGS))
        }
        findViewById<Button>(R.id.batteryButton).setOnClickListener {
            requestBatteryExemption()
        }

        log("aicompanion relay -- configure host + secret, then Start")
    }

    // bindService() can return false (e.g. the service isn't running and
    // these flags don't auto-create it) without throwing -- calling
    // unbindService() after a bind that returned false throws
    // IllegalArgumentException: Service not registered. isBound tracks
    // which case actually happened so onStop() only unbinds a real bind.
    private var isBound = false

    override fun onStart() {
        super.onStart()
        isBound = bindService(Intent(this, RelayService::class.java), connection, 0)
    }

    override fun onStop() {
        super.onStop()
        relay?.logListener = null
        relay?.settingsListener = null
        relay?.otaListener = null
        if (isBound) {
            unbindService(connection)
            isBound = false
        }
    }

    private fun startRelay() {
        prefs.bridgeHost = hostInput.text.toString().trim()
        prefs.bridgePort = portInput.text.toString().trim().toIntOrNull() ?: prefs.bridgePort
        prefs.sharedSecret = secretInput.text.toString()
        if (prefs.bridgeHost.isBlank()) {
            log("Set a gateway host first (normally ${Prefs.DEFAULT_GATEWAY_HOST})")
            return
        }

        val needed = listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            .let { if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) it + Manifest.permission.POST_NOTIFICATIONS else it }
            .filter { ActivityCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQUEST_PERMS)
            return
        }
        launchRelayService()
    }

    private fun launchRelayService() {
        val intent = Intent(this, RelayService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent) else startService(intent)
        // Only if onStart()'s bind didn't already succeed -- avoids an
        // unmatched second bindService() call against the same connection.
        if (!isBound) {
            isBound = bindService(intent, connection, 0)
        }
    }

    private fun stopRelay() {
        relay?.logListener = null
        relay?.settingsListener = null
        relay?.otaListener = null
        relay?.stopRelay()
        // stopSelf() cannot destroy a started service while this Activity is
        // still bound to it. Drop the binding as part of Stop so onDestroy()
        // can close BLE/WebSocket state and the next Start creates a fresh
        // RelayService instead of reconnecting to the stopped instance.
        if (isBound) {
            unbindService(connection)
            isBound = false
        }
        relay = null
        running = false
        updateButtons()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_PERMS) {
            if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
                launchRelayService()
            } else {
                log("Bluetooth/notification permissions denied -- can't start")
            }
        }
    }

    private fun requestBatteryExemption() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            log("Already exempt from battery optimization")
            return
        }
        startActivity(
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
        )
    }

    private fun showStickInfo(info: RelayService.StickInfo) {
        handler.post {
            firmwareText.text = "Stick firmware: ${info.firmware}"
            volumeBar.progress = info.volume
            brightnessBar.progress = info.brightness
            volumeBar.isEnabled = true
            brightnessBar.isEnabled = true
            updateSettingLabels()
        }
    }

    private fun updateSettingLabels() {
        // M5Stack advises staying under 191 on battery: louder can brown out the Stick.
        val warn = if (volumeBar.progress > 191) "  (over 191: may reboot on battery)" else ""
        volumeLabel.text = "Volume ${volumeBar.progress}/255$warn"
        brightnessLabel.text = "Brightness ${brightnessBar.progress}/255"
    }

    private fun showOta(message: String, percent: Int) {
        handler.post {
            val running = percent in 0..99
            otaProgress.visibility = if (running) View.VISIBLE else View.GONE
            if (percent >= 0) otaProgress.progress = percent
            // A screen timeout mid-transfer is harmless (the service does the
            // work) but keeping it on makes progress visible without unlocking.
            if (running) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            firmwareText.text = "Update: $message"
        }
    }

    private fun updateButtons() {
        startStopButton.text = if (running) "Stop" else "Start"
    }

    private fun log(line: String) {
        handler.post {
            logView.append("\n$line")
            logScroll.post { logScroll.fullScroll(android.view.View.FOCUS_DOWN) }
        }
    }
}
