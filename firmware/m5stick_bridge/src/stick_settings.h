// Volume and brightness as live settings, so changing them no longer means a
// rebuild and a USB flash. The phone sends a SETTINGS frame; the Stick applies
// it on its next loop() (not in the BLE callback: the display and speaker are
// driven from loop(), and touching them from the NimBLE task would race it),
// saves it in NVS so it survives a reboot, and reports back what it applied
// together with its firmware version, which is also how the app learns the
// current values after connecting.
//
// Wire format, both directions: [format=1][volume 0-255][brightness 0-255],
// and from the Stick only, the firmware version string after those 3 bytes.
#pragma once
#include <Preferences.h>

#ifndef FW_VERSION
#define FW_VERSION "unknown"
#endif

namespace StickSettings {

// Defaults are the values that used to be hardcoded in setup().
static const uint8_t DEFAULT_VOLUME = 255;
static const uint8_t DEFAULT_BRIGHTNESS = 38;
// A black screen looks like a dead Stick, and there'd be no way to see the
// app's slider had done it; keep the backlight at least faintly on.
static const uint8_t MIN_BRIGHTNESS = 8;

static uint8_t volume = DEFAULT_VOLUME;
static uint8_t brightness = DEFAULT_BRIGHTNESS;

static volatile bool pending = false;
static volatile uint8_t pendingVolume = 0, pendingBrightness = 0;
static volatile bool reportDue = false;

static void apply() {
  M5.Speaker.setVolume(volume);
  M5.Display.setBrightness(brightness);
}

static void load() {
  Preferences prefs;
  prefs.begin("stick", true);
  volume = prefs.getUChar("volume", DEFAULT_VOLUME);
  brightness = max(prefs.getUChar("brightness", DEFAULT_BRIGHTNESS), MIN_BRIGHTNESS);
  prefs.end();
  apply();
  Serial.printf("[settings] volume=%u brightness=%u firmware=%s\n", volume, brightness, FW_VERSION);
}

// NimBLE task: just record the request.
static void onFrame(const uint8_t *payload, size_t len) {
  if (len < 3 || payload[0] != 1) {
    Serial.printf("[settings] malformed frame, len=%u\n", (unsigned)len);
    return;
  }
  pendingVolume = payload[1];
  pendingBrightness = payload[2];
  pending = true;
}

// Called once per loop(). Applies a pending change, and sends the report when
// one is due (after a change, or when the link comes up).
static void tick(bool linkUp) {
  static bool wasUp = false;
  if (linkUp && !wasUp) reportDue = true;
  wasUp = linkUp;

  if (pending) {
    pending = false;
    volume = (uint8_t)pendingVolume;
    brightness = max((uint8_t)pendingBrightness, MIN_BRIGHTNESS);
    apply();
    Preferences prefs;
    prefs.begin("stick", false);
    prefs.putUChar("volume", volume);
    prefs.putUChar("brightness", brightness);
    prefs.end();
    Serial.printf("[settings] applied volume=%u brightness=%u\n", volume, brightness);
    reportDue = true;
  }
  if (reportDue && linkUp) {
    uint8_t buf[3 + 48];
    size_t vlen = strnlen(FW_VERSION, 48);
    buf[0] = 1;
    buf[1] = volume;
    buf[2] = brightness;
    memcpy(buf + 3, FW_VERSION, vlen);
    if (BleTransport::sendFrame(BleEnvelope::FRAME_SETTINGS, buf, 3 + vlen)) reportDue = false;
  }
}

}  // namespace StickSettings
