package com.aicompanion.relay

import android.content.Context

/** Persistent relay settings. The k3s gateway is the normal endpoint; host
 * and port remain editable so bridge_server.py can still be used as a local
 * fallback without rebuilding either the app or the firmware. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("relay_prefs", Context.MODE_PRIVATE)

    companion object {
        const val DEFAULT_GATEWAY_HOST = "gateway.your-tailnet.ts.net"
        const val DEFAULT_GATEWAY_PORT = 30800
        private const val ENDPOINT_DEFAULTS_VERSION = 1
    }

    init {
        // One-time cutover for an already-installed companion app. Preserve
        // the old endpoint so it is recoverable for troubleshooting, but make
        // the always-on k3s gateway the active path after this app update.
        if (sp.getInt("endpoint_defaults_version", 0) < ENDPOINT_DEFAULTS_VERSION) {
            val oldHost = sp.getString("bridge_host", null)
            val oldPort = sp.getInt("bridge_port", 8765)
            sp.edit().apply {
                if (!oldHost.isNullOrBlank()) putString("legacy_bridge_host", oldHost)
                putInt("legacy_bridge_port", oldPort)
                putString("bridge_host", DEFAULT_GATEWAY_HOST)
                putInt("bridge_port", DEFAULT_GATEWAY_PORT)
                putInt("endpoint_defaults_version", ENDPOINT_DEFAULTS_VERSION)
            }.apply()
        }
    }

    var bridgeHost: String
        get() = sp.getString("bridge_host", DEFAULT_GATEWAY_HOST) ?: DEFAULT_GATEWAY_HOST
        set(value) = sp.edit().putString("bridge_host", value).apply()

    var bridgePort: Int
        get() = sp.getInt("bridge_port", DEFAULT_GATEWAY_PORT)
        set(value) = sp.edit().putInt("bridge_port", value).apply()

    // Defaults to main.cpp's placeholder so a fresh install and fresh flash
    // interoperate. Change both sides together before treating it as a secret.
    var sharedSecret: String
        get() = sp.getString("shared_secret", "spike-shared-secret-change-me") ?: ""
        set(value) = sp.edit().putString("shared_secret", value).apply()
}
