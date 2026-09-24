// Firmware updates over BLE, from the companion app, so a new build no longer
// needs the Stick plugged into a computer.
//
// Needs a two-slot partition table (platformio.ini: default_8MB.csv, two
// 3.2 MB app slots): the update is written into the slot that is not running,
// checked, and only then made the boot slot. The running firmware is never
// touched, so a transfer that fails or is cut off costs nothing.
//
// Protocol (frame types in ble_envelope.h), only accepted after AUTH -- the
// shared secret is what stops anyone else in range from flashing the Stick:
//   phone OTA_BEGIN  [u32 size LE][32 ASCII hex chars: MD5 of the image]
//   Stick OTA_STATUS READY
//   phone OTA_DATA   [u32 offset LE][bytes]   ... in order, windowed:
//   Stick OTA_STATUS PROGRESS(bytes written to flash) every ACK_EVERY bytes;
//                    the phone keeps at most its window of unacknowledged data
//                    in flight, so the staging buffer below can never overflow
//   phone OTA_END
//   Stick OTA_STATUS DONE, then reboots into the new slot   (or ERROR + text)
// OTA_STATUS payload: [code][u32 value LE][UTF-8 message].
//
// Rollback: the bootloader has CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE, so a new
// image boots "pending verify". It is only marked good once a phone has
// connected and passed AUTH on it -- which proves the part that matters (the
// shared secret compiled into it matches the app's). If it crashes before
// that, or never gets an AUTH within VERIFY_TIMEOUT_MS, the Stick goes back
// to the previous firmware by itself.
#pragma once
#include <Update.h>
#include <esp_ota_ops.h>
#include <freertos/stream_buffer.h>

// Arduino-ESP32 would otherwise mark every new image valid at boot, before
// anything has been verified (esp32-hal-misc.c, initArduino).
extern "C" bool verifyRollbackLater() { return true; }

namespace OtaUpdate {

enum Status : uint8_t { READY = 1, PROGRESS = 2, DONE = 3, ERROR = 4 };
enum State { IDLE, RECEIVING, FINISHED };

static const size_t STAGING_BYTES = 64 * 1024;  // phone's window is 24 KB, well inside this
static const size_t ACK_EVERY = 8 * 1024;
static const uint32_t VERIFY_TIMEOUT_MS = 10UL * 60 * 1000;

static volatile State state = IDLE;
static StreamBufferHandle_t staging = nullptr;
static StaticStreamBuffer_t stagingStruct;
static uint8_t *stagingStorage = nullptr;

// Written by the NimBLE task, read by loop().
static volatile bool beginRequested = false, endRequested = false;
static volatile uint32_t requestedSize = 0;
static char requestedMd5[33] = {0};
static volatile uint32_t received = 0;     // bytes accepted into staging
static volatile bool callbackError = false;
static char callbackErrorText[64] = {0};

// loop()-only.
static uint32_t totalSize = 0, written = 0, lastAck = 0, rebootAt = 0;
static bool pendingVerify = false;

static void sendStatus(Status code, uint32_t value, const char *msg = "") {
  uint8_t buf[5 + 96];
  size_t mlen = strnlen(msg, 96);
  buf[0] = code;
  memcpy(buf + 1, &value, 4);  // little-endian on this platform
  memcpy(buf + 5, msg, mlen);
  BleTransport::sendFrame(BleEnvelope::FRAME_OTA_STATUS, buf, 5 + mlen);
}

static void fail(const char *why) {
  Serial.printf("[ota] failed: %s\n", why);
  if (Update.isRunning()) Update.abort();
  state = IDLE;
  sendStatus(ERROR, written, why);
}

static bool active() { return state != IDLE; }

static uint8_t percent() { return totalSize ? (uint8_t)((uint64_t)written * 100 / totalSize) : 0; }

static void begin() {
  stagingStorage = (uint8_t *)ps_malloc(STAGING_BYTES + 1);
  if (stagingStorage)
    staging = xStreamBufferCreateStatic(STAGING_BYTES, 1, stagingStorage, &stagingStruct);

  const esp_partition_t *running = esp_ota_get_running_partition();
  esp_ota_img_states_t st;
  if (esp_ota_get_state_partition(running, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
    pendingVerify = true;
    Serial.printf("[ota] running a new image from %s, pending verification (needs AUTH within %lus)\n",
                  running->label, (unsigned long)(VERIFY_TIMEOUT_MS / 1000));
  }
}

// NimBLE task. Only records and stages; flash writes happen in tick().
static void onFrame(uint8_t type, const uint8_t *payload, size_t len) {
  switch (type) {
    case BleEnvelope::FRAME_OTA_BEGIN:
      if (len != 4 + 32) return;
      memcpy((void *)&requestedSize, payload, 4);
      memcpy(requestedMd5, payload + 4, 32);
      requestedMd5[32] = 0;
      beginRequested = true;
      break;
    case BleEnvelope::FRAME_OTA_DATA: {
      if (state != RECEIVING || callbackError || len <= 4) return;
      uint32_t offset;
      memcpy(&offset, payload, 4);
      if (offset != received) {
        snprintf(callbackErrorText, sizeof callbackErrorText, "data out of order: got %lu, expected %lu",
                 (unsigned long)offset, (unsigned long)received);
        callbackError = true;
        return;
      }
      size_t n = len - 4;
      if (xStreamBufferSend(staging, payload + 4, n, 0) != n) {
        snprintf(callbackErrorText, sizeof callbackErrorText, "staging buffer overflow at %lu",
                 (unsigned long)offset);
        callbackError = true;
        return;
      }
      received = received + n;
      break;
    }
    case BleEnvelope::FRAME_OTA_END:
      endRequested = true;
      break;
  }
}

// Once per loop().
static void tick(bool linkUp) {
  if (pendingVerify) {
    if (linkUp) {
      esp_ota_mark_app_valid_cancel_rollback();
      pendingVerify = false;
      Serial.println("[ota] new firmware verified (AUTH passed); rollback cancelled");
    } else if (millis() > VERIFY_TIMEOUT_MS) {
      Serial.println("[ota] no AUTH on the new firmware; rolling back");
      esp_ota_mark_app_invalid_rollback_and_reboot();
    }
  }

  if (state == FINISHED) {
    if (millis() > rebootAt) ESP.restart();
    return;
  }
  if (beginRequested) {
    beginRequested = false;
    endRequested = false;
    if (!staging) { fail("no staging buffer (PSRAM)"); return; }
    if (Update.isRunning()) Update.abort();
    xStreamBufferReset(staging);
    totalSize = requestedSize;
    written = lastAck = 0;
    received = 0;
    callbackError = false;
    if (!Update.begin(totalSize, U_FLASH)) { fail(Update.errorString()); return; }
    Update.setMD5(requestedMd5);
    state = RECEIVING;
    Serial.printf("[ota] receiving %lu bytes, md5 %s\n", (unsigned long)totalSize, requestedMd5);
    sendStatus(READY, totalSize);
    return;
  }
  if (state != RECEIVING) return;
  if (!linkUp) { fail("link lost"); return; }
  if (callbackError) { fail(callbackErrorText); return; }

  // Bounded per call so the progress screen and buttons stay responsive.
  static uint8_t chunk[4096];
  for (int i = 0; i < 8; i++) {
    size_t n = xStreamBufferReceive(staging, chunk, sizeof chunk, 0);
    if (n == 0) break;
    if (Update.write(chunk, n) != n) { fail(Update.errorString()); return; }
    written += n;
  }
  if (written - lastAck >= ACK_EVERY || (written == totalSize && lastAck != written)) {
    lastAck = written;
    sendStatus(PROGRESS, written);
  }
  if (endRequested && written >= totalSize) {
    endRequested = false;
    if (!Update.end()) { fail(Update.errorString()); return; }  // checks size and MD5
    Serial.println("[ota] image verified, rebooting into it");
    sendStatus(DONE, written);
    state = FINISHED;
    rebootAt = millis() + 1500;  // let DONE reach the phone first
  }
}

}  // namespace OtaUpdate
