// Phase 3 protocol spike, part 2: adds bonding + the app-layer shared-secret
// AUTH handshake + TIME_SYNC on top of the byte-envelope codec that was
// already confirmed round-tripping on real hardware (2026-09-16, see
// docs/ble-migration.md). Deliberately built in that order -- no point
// securing/time-syncing a channel that hadn't been proven to work yet.
//
// Security model (per the plan's Phase 3 section, adjusted after real
// hardware testing -- see the note below): bonding alone isn't enough on
// its own -- a bonded-but-wrong-secret peer (e.g. after the phone re-pairs
// post factory-reset) still shouldn't get real data, so there are two
// independent gates:
//   1. Bonding (setSecurityAuth below) -- an unbonded eavesdropper can't
//      even subscribe usefully, since nothing meaningful goes out on TX
//      pre-auth either (see appAuthed below).
//   2. appAuthed -- true only after a valid AUTH frame (the right shared
//      secret) is received. Bonding proves "this is a device I've paired
//      with before"; AUTH proves "and it actually knows the secret", which
//      is what a stolen-but-still-bonded phone, or a bonding-database
//      mixup, wouldn't have. This is the gate that actually does the real
//      access-control work, independent of whatever the BLE link layer's
//      own encryption state is.
//
// RX is plain NIMBLE_PROPERTY::WRITE, not WRITE_ENC as originally designed.
// WRITE_ENC was tried first and reproducibly failed on real hardware: even
// with bonding genuinely complete (confirmed on the Stick's own serial log,
// onAuthenticationComplete firing with encrypted=true, before any write was
// attempted) and 5 retries with a 300ms backoff, Android's writeCharacteristic()
// synchronously returned ERROR_GATT_WRITE_NOT_ALLOWED (200) every single
// time -- a persistent local rejection by Android's own GATT client, not a
// transient encryption-settling race (which a working retry would have
// cleared). Root cause not fully isolated (likely an Android/OEM-side
// permission check disagreeing with the peripheral's own encryption state
// for a freshly-bonded reconnect), but not worth chasing further: appAuthed
// already does the real security work regardless of whether the ATT layer
// itself mandates encryption for this one characteristic, and bonding still
// happens and still encrypts the link when both sides support it -- this
// characteristic just no longer *requires* that encryption to accept a
// write. **Confirmed on real hardware (2026-09-16)**: with plain WRITE,
// AUTH/TIME_SYNC/HEARD all succeeded on the first attempt after a fresh
// pair, and STATUS frames resumed immediately once appAuthed flipped true
// -- see docs/ble-migration.md for the full log, including the two stale-
// bond dead ends hit along the way (a phone-side bond-broadcast receiver
// needing RECEIVER_EXPORTED, and a stale bond record needing a manual
// unpair on the phone after repeated reflashing left the two sides'
// stored keys disagreeing).
//
// No MITM protection (setSecurityAuth's second arg): Just Works pairing,
// deliberately -- this is a personal device paired once in private, not a
// public one where an active pairing-time attacker is a realistic threat,
// and the AUTH secret is the layer actually doing the "prove you're
// supposed to be here" work. IO cap is NoInputNoOutput to match (no
// keyboard/display on this hardware to enter or show a passkey anyway).
//
// TIME_SYNC and AUTH payload formats (not specified in the plan doc, chosen
// here -- must match android_companion/.../MainActivity.kt exactly):
//   AUTH payload:       UTF-8 shared-secret string, no extra framing.
//   TIME_SYNC payload:  8 bytes, little-endian int64, Unix epoch seconds.
#include <Arduino.h>
#include <M5Unified.h>
#include <NimBLEDevice.h>

#include "ble_envelope.h"

// Same service UUID as Phase 2 (freshly generated, not a well-known one --
// see the history in git log for why that mattered). Must match
// android_companion/.../MainActivity.kt exactly.
static const char *SVC_UUID     = "667d22e3-b922-4852-b7a6-6a4764a26665";
static const char *TX_CHAR_UUID = "c0819f6b-f0b7-4db8-9dcb-9f58b2745f9c";
static const char *RX_CHAR_UUID = "9e5d1e40-6b0a-4b7a-9c2e-5b7c9a6a0e11";

// Spike-only placeholder -- the real firmware/m5stick_bridge integration
// (Phase 4) must move this into secrets.h alongside WIFI_SSID/WIFI_PASS's
// eventual BLE-era replacements, not hardcode it in source that's checked
// into git. Fine here: this whole directory is a throwaway spike, and the
// value itself is never meant to protect anything real.
static const char *SHARED_SECRET = "spike-shared-secret-change-me";

static NimBLECharacteristic *txChar = nullptr;
static volatile bool connected = false;
static volatile bool appAuthed = false;
static uint16_t gConnHandle = 0;

// PacketSink: hands each physical packet from BleEnvelope::encode() to a
// real notify() call. Matches PacketSink's contract -- false on failure so
// encode() stops mid-frame instead of pushing packets into a queue no one's
// draining.
class NotifySink : public BleEnvelope::PacketSink {
 public:
  bool send(const uint8_t *packet, size_t len) override {
    if (!connected || !txChar) return false;
    return txChar->notify(packet, len, gConnHandle);
  }
};
static NotifySink notifySink;

static bool isAuthPayload(const uint8_t *payload, size_t len) {
  size_t secretLen = strlen(SHARED_SECRET);
  return len == secretLen && memcmp(payload, SHARED_SECRET, secretLen) == 0;
}

// FrameSink: logs whatever the phone writes and got fully reassembled, and
// is the one place appAuthed is set/checked -- every frame type except AUTH
// itself is dropped until a valid AUTH frame has been seen on this
// connection. Payload isn't guaranteed NUL-terminated (it's a raw byte
// buffer sized to exactly `len`), so text frames are always logged with an
// explicit length, never treated as a C string.
class RxLogSink : public BleEnvelope::FrameSink {
 public:
  void onFrame(uint8_t type, const uint8_t *payload, size_t len) override {
    if (type == BleEnvelope::FRAME_AUTH) {
      appAuthed = isAuthPayload(payload, len);
      Serial.printf("[spike] AUTH frame: %s\n", appAuthed ? "accepted" : "REJECTED");
      return;
    }
    if (!appAuthed) {
      Serial.printf("[spike] RX frame type=0x%02X len=%u dropped -- not authed yet\n",
                    type, (unsigned)len);
      return;
    }
    if (type == BleEnvelope::FRAME_TIME_SYNC) {
      if (len != 8) {
        Serial.printf("[spike] TIME_SYNC malformed, len=%u (want 8)\n", (unsigned)len);
        return;
      }
      int64_t epochSec = 0;
      memcpy(&epochSec, payload, 8);  // little-endian, matches this platform's native order
      Serial.printf("[spike] TIME_SYNC: epoch=%lld\n", (long long)epochSec);
      return;
    }
    Serial.printf("[spike] RX frame type=0x%02X len=%u: %.*s\n",
                  type, (unsigned)len, (int)len, (const char *)payload);
  }
};
static RxLogSink rxLogSink;
static BleEnvelope::Decoder rxDecoder(rxLogSink);

class RxCharCB : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *c, NimBLEConnInfo &info) override {
    NimBLEAttValue v = c->getValue();  // NimBLEAttValue, not std::string -- verified against
    rxDecoder.feed(v.data(), v.length());  // the installed library's own header, not assumed
  }
};

class TxCharCB : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &info, uint16_t subValue) override {
    Serial.printf("[spike] onSubscribe: subValue=%u\n", subValue);
  }
};

class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *server, NimBLEConnInfo &info) override {
    gConnHandle = info.getConnHandle();
    Serial.printf("[spike] connected, handle=%u\n", gConnHandle);

    // Tuning, applied once per connection -- carried over from Phase 2.
    // Interval units are 1.25ms (BLE spec), timeout units are 10ms:
    // 6*1.25=7.5ms min, 12*1.25=15ms max, 400*10ms=4s supervision timeout.
    server->updatePhy(gConnHandle, BLE_GAP_LE_PHY_2M, BLE_GAP_LE_PHY_2M, 0);
    server->setDataLen(gConnHandle, 251);  // max LL payload octets
    server->updateConnParams(gConnHandle, 6, 12, 0, 400);

    appAuthed = false;  // every new connection must AUTH again, bonded or not
    connected = true;   // set last -- loop() starts notifying as soon as this flips
  }
  void onDisconnect(NimBLEServer *server, NimBLEConnInfo &info, int reason) override {
    connected = false;
    appAuthed = false;
    Serial.printf("[spike] disconnected, reason=%d\n", reason);
    NimBLEDevice::startAdvertising();
  }
  void onMTUChange(uint16_t mtu, NimBLEConnInfo &info) override {
    Serial.printf("[spike] MTU negotiated: %u\n", mtu);
  }
  void onAuthenticationComplete(NimBLEConnInfo &info) override {
    Serial.printf("[spike] pairing complete: bonded=%d encrypted=%d authenticated=%d\n",
                  info.isBonded(), info.isEncrypted(), info.isAuthenticated());
  }
};

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  Serial.begin(115200);
  delay(300);
  Serial.println("[spike] Phase 3 protocol spike starting");

  // init() must come before setMTU()/setDefaultPhy()/security setup -- all
  // call directly into the NimBLE host stack, which doesn't exist yet
  // otherwise (crashed at boot with a null host mutex assert the one time
  // this was tried first, back in Phase 2).
  NimBLEDevice::init("aigf-ble-spike");
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
  // Plain WRITE, not WRITE_ENC -- see the header comment for why. appAuthed
  // (checked in RxLogSink::onFrame) is the real gate, not the ATT layer.
  NimBLECharacteristic *rxChar =
      service->createCharacteristic(RX_CHAR_UUID, NIMBLE_PROPERTY::WRITE);
  rxChar->setCallbacks(new RxCharCB());
  service->start();

  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->start();
  Serial.println("[spike] advertising");
}

static const uint32_t STATUS_INTERVAL_MS = 200;
static const uint32_t LONG_STATUS_EVERY = 25;  // every 25th tick (~5s) -- forces continuation frames

void loop() {
  static uint32_t lastTickMs = 0;
  static uint32_t tick = 0;

  uint32_t now = millis();
  // appAuthed, not just connected -- nothing real goes out pre-AUTH, the
  // other half of the "bonded but wrong secret still can't stream" gate.
  if (connected && appAuthed && (now - lastTickMs >= STATUS_INTERVAL_MS)) {
    lastTickMs = now;
    tick++;

    char msg[600];
    size_t msgLen;
    if (tick % LONG_STATUS_EVERY == 0) {
      // Padded well past MAX_FIRST_PAYLOAD (497 B) so this can only have
      // gone through on real hardware if the continuation path works, not
      // just the common single-packet case.
      int n = snprintf(msg, sizeof(msg), "alive tick=%u ", tick);
      while (n < 550 && n < (int)sizeof(msg) - 1) msg[n++] = 'x';
      msg[n] = '\0';
      msgLen = n;
    } else {
      msgLen = snprintf(msg, sizeof(msg), "alive tick=%u", tick);
    }

    if (!BleEnvelope::encode(BleEnvelope::FRAME_STATUS, (const uint8_t *)msg, msgLen, notifySink)) {
      Serial.println("[spike] encode/send failed (no subscriber yet?)");
    }
  } else {
    delay(1);
  }
}
