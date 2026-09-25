// Battery reports for the phone (and from there the gateway, Prometheus and
// the agent), so power draw can be measured by rundown instead of guessed:
// once a minute, and as soon as the link comes up.
//
// Wire format (FRAME_BATTERY, Stick -> phone only): [format=1]
// [percent 0-100, 255 unknown][charging 0/1, 255 unknown]
// [millivolts, 2 bytes little-endian, 0 unknown].
#pragma once

namespace BatteryReport {

static const uint32_t INTERVAL_MS = 60000;

static void tick(bool linkUp) {
  static bool wasUp = false;
  static uint32_t lastSentMs = 0;
  bool due = (linkUp && !wasUp) || (linkUp && millis() - lastSentMs >= INTERVAL_MS);
  wasUp = linkUp;
  if (!due) return;

  int level = M5.Power.getBatteryLevel();
  int charging = (int)M5.Power.isCharging();  // Power_Class::is_charging_t: 0 no, 1 yes, 2 unknown
  int mv = M5.Power.getBatteryVoltage();
  uint8_t buf[5] = {1, (uint8_t)(level >= 0 && level <= 100 ? level : 255),
                    (uint8_t)(charging == 0 || charging == 1 ? charging : 255),
                    (uint8_t)(mv > 0 ? mv & 0xFF : 0), (uint8_t)(mv > 0 ? (mv >> 8) & 0xFF : 0)};
  if (BleTransport::sendFrame(BleEnvelope::FRAME_BATTERY, buf, sizeof(buf))) {
    lastSentMs = millis();
    Serial.printf("[battery] %d%% %d mV charging=%d\n", level, mv, charging);
  }
}

}  // namespace BatteryReport
