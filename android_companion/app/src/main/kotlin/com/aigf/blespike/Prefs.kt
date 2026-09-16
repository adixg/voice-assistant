package com.aigf.blespike

import android.content.Context

/** Tiny SharedPreferences wrapper for the settings the plan calls for:
 * "laptop host selection, shared-secret entry" -- kept to exactly those two
 * plus the port, since everything else (service/characteristic UUIDs) is a
 * protocol constant, not a per-install setting. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("relay_prefs", Context.MODE_PRIVATE)

    var bridgeHost: String
        get() = sp.getString("bridge_host", "") ?: ""
        set(value) = sp.edit().putString("bridge_host", value).apply()

    var bridgePort: Int
        get() = sp.getInt("bridge_port", 8765)
        set(value) = sp.edit().putInt("bridge_port", value).apply()

    // Defaults to main.cpp's own placeholder so a fresh install matches the
    // spike firmware out of the box; both sides still need changing before
    // this is anything but a placeholder -- see main.cpp's SHARED_SECRET
    // comment.
    var sharedSecret: String
        get() = sp.getString("shared_secret", "spike-shared-secret-change-me") ?: ""
        set(value) = sp.edit().putString("shared_secret", value).apply()
}
