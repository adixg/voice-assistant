// Volume and brightness as live settings, so changing them no longer means a
// rebuild and a USB flash. The phone sends a SETTINGS frame; the Stick applies
// it on its next loop() (not in the BLE callback: the display and speaker are
// driven from loop(), and touching them from the NimBLE task would race it),
// saves it in NVS so it survives a reboot, and reports back what it applied
// together with its firmware version, which is also how the app learns the
// current values after connecting.
//
// Wire format, both directions: [format=1][volume 0-255][brightness 0-255,
// 0 = screen off],
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

// Brightness 0 means screen off, for battery: the backlight is off, the panel
// asleep, and main.cpp skips drawing. It looks dead on purpose. A button tap
// wakes it ("peek") at the last brightness it was on at, for PEEK_MS, then it
// goes dark again; the tap that wakes it does nothing else (main.cpp).
static const uint32_t PEEK_MS = 10000;

static uint8_t volume = DEFAULT_VOLUME;
static uint8_t brightness = DEFAULT_BRIGHTNESS;
static uint8_t lastOnBrightness = DEFAULT_BRIGHTNESS;  // what a peek shows
static uint32_t peekUntil = 0;       // 0 = not peeking
static bool panelAsleep = false;
static bool repaintDue = false;      // the screen just came back: redraw it all

static bool screenOff() { return brightness == 0; }
// Whether anything should be drawn at all this loop.
static bool screenVisible() { return brightness != 0 || peekUntil != 0; }

static void setPanel(bool on, uint8_t level) {
  if (on) {
    if (panelAsleep) {
      M5.Display.wakeup();
      panelAsleep = false;
      repaintDue = true;
    }
    M5.Display.setBrightness(level);
  } else {
    M5.Display.setBrightness(0);
    if (!panelAsleep) {
      M5.Display.sleep();
      panelAsleep = true;
    }
  }
}

// A button tap while the screen is off. Returns true if it woke the screen,
// in which case the caller should not also act on the tap. While already
// peeking, a tap acts normally and restarts the peek timer.
static bool peek() {
  if (!screenOff()) return false;
  bool wasDark = peekUntil == 0;
  peekUntil = millis() + PEEK_MS;
  if (peekUntil == 0) peekUntil = 1;
  if (wasDark) setPanel(true, lastOnBrightness);
  return wasDark;
}

// True once after the screen came back on; main.cpp then invalidates its faces.
static bool takeRepaint() {
  bool due = repaintDue;
  repaintDue = false;
  return due;
}

static volatile bool pending = false;
static volatile uint8_t pendingVolume = 0, pendingBrightness = 0;
static volatile bool reportDue = false;

static void apply() {
  M5.Speaker.setVolume(volume);
  if (brightness) {
    lastOnBrightness = brightness;
    peekUntil = 0;
    setPanel(true, brightness);
  } else if (!peekUntil) {
    setPanel(false, 0);
  }
}

static void load() {
  Preferences prefs;
  prefs.begin("stick", true);
  volume = prefs.getUChar("volume", DEFAULT_VOLUME);
  brightness = prefs.getUChar("brightness", DEFAULT_BRIGHTNESS);
  lastOnBrightness = prefs.getUChar("bright_on", DEFAULT_BRIGHTNESS);
  if (!lastOnBrightness) lastOnBrightness = DEFAULT_BRIGHTNESS;
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

  if (peekUntil && (int32_t)(millis() - peekUntil) >= 0) {
    peekUntil = 0;
    if (screenOff()) setPanel(false, 0);
  }

  if (pending) {
    pending = false;
    volume = (uint8_t)pendingVolume;
    brightness = (uint8_t)pendingBrightness;
    apply();
    Preferences prefs;
    prefs.begin("stick", false);
    prefs.putUChar("volume", volume);
    prefs.putUChar("brightness", brightness);
    prefs.putUChar("bright_on", lastOnBrightness);
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
