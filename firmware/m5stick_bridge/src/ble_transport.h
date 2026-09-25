// Phase 4: BLE transport, replacing WiFi.h + WebSocketsClient. Adapted from
// the Phase 3 spike (firmware/m5stick_ble_flash_spike/src/main.cpp) once
// bonding, the app-layer AUTH handshake, TIME_SYNC, and the codec were all
// confirmed round-tripping together on real hardware (2026-09-16) -- see
// docs/ble-migration.md for that full log, including the two real bugs
// found getting there (an Android bond-broadcast receiver needing
// RECEIVER_EXPORTED, and WRITE_ENC on the RX characteristic reproducibly
// rejected by Android's stack regardless of retries).
//
// Included from main.cpp after its state globals (uiState, captionText,
// replyBuf/replyLen/replyCap/receivingReply, setStatus(), showTransient(),
// SAMPLE_RATE) and clock_face.h's CLOCK_TZ are all declared -- this header
// uses them directly, the same way clock_face.h/pomodoro_face.h use
// canvas/COL_* directly, rather than introducing a callback-registration
// API this codebase doesn't otherwise use.
//
// Two-gate security model (unchanged from the spike -- see that file's git
// history for the full reasoning and the real-hardware finding that ruled
// out WRITE_ENC): bonding (Just Works, no MITM -- the AUTH secret is the
// real access-control layer, not pairing-time protection) plus an
// app-layer shared-secret AUTH frame. BLE_SHARED_SECRET lives in secrets.h,
// not hardcoded here, unlike the spike (which was throwaway by design).
//
// Protocol split (see ble_envelope.h for the wire format): TX carries
// START/STOP/RESET/AUDIO_CHUNK, exposed here as sendStart()/sendStop()/
// sendReset()/sendAudioChunk() for main.cpp's button/mic code to call,
// replacing the old webSocket.sendTXT()/sendBIN() calls. RX's AUTH and
// TIME_SYNC are handled entirely here (transport-level, not app protocol);
// HEARD/STATUS/REPLY/AUDIO_CHUNK/END are forwarded to handleBleFrame(),
// which main.cpp defines -- the direct replacement for the old
// webSocketEvent()'s WStype_TEXT/WStype_BIN handling.
#pragma once
#include <Arduino.h>
#include <NimBLEDevice.h>
#include <sys/time.h>

#include "ble_envelope.h"
#include "secrets.h"

// main.cpp defines this -- the app-protocol half of what webSocketEvent()
// used to do in one function (the transport half, AUTH/TIME_SYNC, stays in
// this file). Only ever called once bleAuthed is true.
extern void handleBleFrame(uint8_t type, const uint8_t *payload, size_t len);

namespace BleTransport {

// Freshly generated (see git log), not a well-known UUID -- matches
// android_companion/.../MainActivity.kt/RelayService.kt exactly. This is
// the identical service the Phase 3 spike proved end to end, so the
// already-installed Android app needs zero changes for this firmware to
// replace that spike as what it talks to.
static const char *SVC_UUID     = "667d22e3-b922-4852-b7a6-6a4764a26665";
static const char *TX_CHAR_UUID = "c0819f6b-f0b7-4db8-9dcb-9f58b2745f9c";
static const char *RX_CHAR_UUID = "9e5d1e40-6b0a-4b7a-9c2e-5b7c9a6a0e11";

static NimBLECharacteristic *txChar = nullptr;
static volatile bool bleConnected = false;
static volatile bool bleAuthed = false;
static uint16_t gConnHandle = 0;
// The connection parameters the phone last applied (onConnect and
// onConnParamsUpdate), which setFastLink() below compares with what it wants.
static volatile uint16_t linkInterval = 0, linkLatency = 0;
static uint32_t linkRequestMs = 0;

// Hands each physical packet from BleEnvelope::encode() to a real notify()
// call -- false on failure (no subscriber yet, stack's notify queue full)
// so encode() stops mid-frame instead of pushing packets nobody's reading.
class NotifySink : public BleEnvelope::PacketSink {
 public:
  bool send(const uint8_t *packet, size_t len) override {
    if (!bleConnected || !txChar) return false;
    return txChar->notify(packet, len, gConnHandle);
  }
};
static NotifySink notifySink;

static bool isAuthPayload(const uint8_t *payload, size_t len) {
  size_t secretLen = strlen(BLE_SHARED_SECRET);
  return len == secretLen && memcmp(payload, BLE_SHARED_SECRET, secretLen) == 0;
}

// TIME_SYNC replaces NTP entirely -- main.cpp has no network path to
// pool.ntp.org any more. CLOCK_TZ (clock_face.h) still needs applying so
// getLocalTime()/localtime() compute the right local time from the UTC
// epoch this sets; begin() below does that once via setenv+tzset, exactly
// what configTzTime() used to do alongside starting SNTP.
static void applyTimeSync(const uint8_t *payload, size_t len) {
  if (len != 8) {
    Serial.printf("[ble] TIME_SYNC malformed, len=%u (want 8)\n", (unsigned)len);
    return;
  }
  int64_t epochSec = 0;
  memcpy(&epochSec, payload, 8);  // little-endian, matches this platform's native order
  struct timeval tv = {.tv_sec = (time_t)epochSec, .tv_usec = 0};
  settimeofday(&tv, nullptr);
  Serial.printf("[ble] TIME_SYNC applied: epoch=%lld\n", (long long)epochSec);
}

class RxFrameSink : public BleEnvelope::FrameSink {
 public:
  void onFrame(uint8_t type, const uint8_t *payload, size_t len) override {
    if (type == BleEnvelope::FRAME_AUTH) {
      bleAuthed = isAuthPayload(payload, len);
      Serial.printf("[ble] AUTH frame: %s\n", bleAuthed ? "accepted" : "REJECTED");
      if (bleAuthed) {
        setStatus("ble connected");
        if (uiState == UI_DISCONNECTED || uiState == UI_CONNECTING) uiState = UI_IDLE;
      }
      return;
    }
    if (!bleAuthed) {
      Serial.printf("[ble] RX frame type=0x%02X len=%u dropped -- not authed yet\n",
                    type, (unsigned)len);
      return;
    }
    if (type == BleEnvelope::FRAME_TIME_SYNC) {
      applyTimeSync(payload, len);
      return;
    }
    handleBleFrame(type, payload, len);
  }
};
static RxFrameSink rxFrameSink;
static BleEnvelope::Decoder rxDecoder(rxFrameSink);

class RxCharCB : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *c, NimBLEConnInfo &info) override {
    NimBLEAttValue v = c->getValue();  // NimBLEAttValue, not std::string -- verified
    rxDecoder.feed(v.data(), v.length());  // against the installed library's own header
  }
};

class TxCharCB : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &info, uint16_t subValue) override {
    Serial.printf("[ble] onSubscribe: subValue=%u\n", subValue);
  }
};

class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *server, NimBLEConnInfo &info) override {
    gConnHandle = info.getConnHandle();
    linkInterval = info.getConnInterval();
    linkLatency = info.getConnLatency();
    linkRequestMs = 0;
    Serial.printf("[ble] connected, handle=%u\n", gConnHandle);
    // No link tuning here. A bonded phone starts encryption the instant it
    // connects, and firing PHY + data-length + connection-parameter updates
    // on top of that stalled the controller: NimBLE reported an HCI timeout
    // (reason=19), dropped the link, and the phone only noticed ~7 s later by
    // supervision timeout -- one to three wasted attempts, 8-35 s, on every
    // reconnect (phone logcat + serial, 2026-09-24). Tuning now waits for
    // onAuthenticationComplete() below.
    bleAuthed = false;  // every new connection must AUTH again, bonded or not
    bleConnected = true;
  }
  void onDisconnect(NimBLEServer *server, NimBLEConnInfo &info, int reason) override {
    bleConnected = false;
    bleAuthed = false;
    Serial.printf("[ble] disconnected, reason=%d\n", reason);
    setStatus("ble disconnected...");
    uiState = UI_DISCONNECTED;
    NimBLEDevice::startAdvertising();
  }
  void onMTUChange(uint16_t mtu, NimBLEConnInfo &info) override {
    Serial.printf("[ble] MTU negotiated: %u\n", mtu);
  }
  void onAuthenticationComplete(NimBLEConnInfo &info) override {
    Serial.printf("[ble] pairing complete: bonded=%d encrypted=%d authenticated=%d\n",
                  info.isBonded(), info.isEncrypted(), info.isAuthenticated());
    if (!info.isEncrypted()) return;
    // Link tuning, once security is settled (see onConnect). Only the
    // connection interval is requested: the phone asks for the 2M PHY itself
    // (RelayService's setPreferredPhy) and Android negotiates data length on
    // its own. Interval units are 1.25 ms, timeout units 10 ms: 7.5-15 ms
    // interval, 4 s supervision timeout -- carried over from Phase 2/3.
    // loop() drops to the idle parameters once nothing is happening.
    linkRequestMs = millis();
    NimBLEDevice::getServer()->updateConnParams(info.getConnHandle(), 6, 12, 0, 400);
  }
  void onConnParamsUpdate(NimBLEConnInfo &info) override {
    linkInterval = info.getConnInterval();
    linkLatency = info.getConnLatency();
    Serial.printf("[ble] link now %.1f ms interval, latency %u\n", info.getConnInterval() * 1.25f,
                  info.getConnLatency());
  }
};

// True once a central is bonded AND has sent the right AUTH secret -- the
// BLE-era replacement for every old `WiFi.status() == WL_CONNECTED` check
// (the clock/pomodoro screens' link icon, the NTP-sync probe).
static bool ready() { return bleConnected && bleAuthed; }

static bool sendFrame(uint8_t type, const uint8_t *payload, size_t len) {
  if (!ready()) return false;
  return BleEnvelope::encode(type, payload, len, notifySink);
}

// Fast (7.5-15 ms, every event) while audio or an OTA moves; idle otherwise:
// 30-50 ms, and the Stick may skip up to 4 events in a row when it has nothing
// to send, so the radio wakes every ~250 ms instead of every ~10 ms. The cost
// is latency on the phone -> Stick side only (a frame can wait up to ~250 ms);
// the Stick can still send at any event. Asked for only while what the phone
// applied differs from what's wanted, and at most every LINK_RETRY_MS: each
// request is a link-layer procedure, one sent while another is still running
// is dropped (the first idle request, sent right after the fast one at
// connect, never took: 2026-09-25), and the phone may settle on something
// else, which isn't worth asking about more often than that.
static const uint32_t LINK_RETRY_MS = 3000;

static void setFastLink(bool fast) {
  if (!ready()) return;
  bool isFast = linkLatency == 0 && linkInterval <= 12;
  bool isIdle = linkLatency > 0;
  if (fast ? isFast : isIdle) return;
  // A fast request goes out at once (a turn is starting); an idle one waits
  // out the retry interval, which also leaves the connect-time one in peace.
  if (!fast && millis() - linkRequestMs < LINK_RETRY_MS) return;
  if (fast && linkRequestMs && millis() - linkRequestMs < 1000) return;
  linkRequestMs = millis();
  if (fast) {
    NimBLEDevice::getServer()->updateConnParams(gConnHandle, 6, 12, 0, 400);
  } else {
    NimBLEDevice::getServer()->updateConnParams(gConnHandle, 24, 40, 4, 400);
  }
  Serial.printf("[ble] asking for the %s link\n", fast ? "fast" : "idle");
}

static void sendEvent(const char *text) {
  sendFrame(BleEnvelope::FRAME_EVENT, (const uint8_t *)text, strnlen(text, 200));
}

static void sendStart() { sendFrame(BleEnvelope::FRAME_START, nullptr, 0); }
static void sendStop() { sendFrame(BleEnvelope::FRAME_STOP, nullptr, 0); }
static void sendReset() { sendFrame(BleEnvelope::FRAME_RESET, nullptr, 0); }
static void sendAudioChunk(const uint8_t *data, size_t len) {
  sendFrame(BleEnvelope::FRAME_AUDIO_CHUNK, data, len);
}

static void begin() {
  setenv("TZ", CLOCK_TZ, 1);  // see applyTimeSync()'s comment -- no more configTzTime()/SNTP
  tzset();

  // init() must come before setMTU()/setDefaultPhy()/security setup -- all
  // call directly into the NimBLE host stack, which doesn't exist yet
  // otherwise (crashed at boot with a null host mutex assert the one time
  // this was tried first, during the Phase 2 spike).
  NimBLEDevice::init("aicompanion-stick");
  NimBLEDevice::setMTU(517);
  NimBLEDevice::setDefaultPhy(BLE_GAP_LE_PHY_2M_MASK, BLE_GAP_LE_PHY_2M_MASK);

  // bonding=true, mitm=false (Just Works -- see header comment), sc=true
  // (LE Secure Connections; both sides here are modern enough to support it).
  NimBLEDevice::setSecurityAuth(true, false, true);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCB());

  NimBLEService *service = server->createService(SVC_UUID);
  txChar = service->createCharacteristic(TX_CHAR_UUID, NIMBLE_PROPERTY::NOTIFY);
  txChar->setCallbacks(new TxCharCB());
  // Plain WRITE, not WRITE_ENC -- see the header comment for why: bleAuthed
  // is the real gate, not the ATT layer, confirmed on real hardware.
  // WRITE_NR too: reply audio arrives as write-without-response. With only
  // acknowledged writes the phone could send one ~500-byte packet per round
  // trip, measured at ~12 KB/s against the speaker's 32 KB/s, so every reply
  // stuttered (2026-09-24). Control frames and AUTH still use WRITE.
  NimBLECharacteristic *rxChar = service->createCharacteristic(
      RX_CHAR_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rxChar->setCallbacks(new RxCharCB());
  service->start();

  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->start();
  Serial.println("[ble] advertising");
}

}  // namespace BleTransport
