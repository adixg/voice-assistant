// Phase 3 protocol spike: extends Phase 2's tuned-link throughput spike
// with the real byte-envelope codec (ble_envelope.h) and a second,
// write-only characteristic, so both directions of the actual protocol get
// exercised -- not just a one-way synthetic payload. Bonding/AUTH/
// TIME_SYNC are the next increment on top of this one; this spike proves
// the codec + two-characteristic split round-trips correctly first, since
// there's no point securing a channel that doesn't work yet.
//
// TX (notify, Stick -> phone): periodic STATUS frames -- a short one every
// tick, and every 5s a padded one long enough to force multi-packet
// reassembly, so both codec paths (single-packet and continuation) get
// exercised on real hardware, not just in isolation.
// RX (write, phone -> Stick): whatever the test Activity writes gets
// decoded and logged over Serial.
//
// See /home/aditya/.claude/plans/tranquil-drifting-stream.md, Phase 3, and
// this directory's own ble_envelope.h for the full design/wire format.
#include <Arduino.h>
#include <M5Unified.h>
#include <NimBLEDevice.h>

#include "ble_envelope.h"

// Same service UUID as Phase 2 (freshly generated, not a well-known one --
// see the history in git log for why that mattered). New characteristic
// UUID for RX since Phase 2 only ever had the one, TX, direction. Must
// match android_companion/.../MainActivity.kt exactly.
static const char *SVC_UUID    = "667d22e3-b922-4852-b7a6-6a4764a26665";
static const char *TX_CHAR_UUID = "c0819f6b-f0b7-4db8-9dcb-9f58b2745f9c";
static const char *RX_CHAR_UUID = "9e5d1e40-6b0a-4b7a-9c2e-5b7c9a6a0e11";

static NimBLECharacteristic *txChar = nullptr;
static volatile bool connected = false;
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

// FrameSink: logs whatever the phone writes and got fully reassembled.
// Payload isn't guaranteed NUL-terminated (it's a raw byte buffer sized to
// exactly `len`), so this always passes length explicitly rather than
// treating it as a C string.
class RxLogSink : public BleEnvelope::FrameSink {
 public:
  void onFrame(uint8_t type, const uint8_t *payload, size_t len) override {
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

    connected = true;  // set last -- loop() starts notifying as soon as this flips
  }
  void onDisconnect(NimBLEServer *server, NimBLEConnInfo &info, int reason) override {
    connected = false;
    Serial.printf("[spike] disconnected, reason=%d\n", reason);
    NimBLEDevice::startAdvertising();
  }
  void onMTUChange(uint16_t mtu, NimBLEConnInfo &info) override {
    Serial.printf("[spike] MTU negotiated: %u\n", mtu);
  }
};

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  Serial.begin(115200);
  delay(300);
  Serial.println("[spike] Phase 3 protocol spike starting");

  // init() must come before setMTU()/setDefaultPhy() -- both call directly
  // into the NimBLE host stack, which doesn't exist yet otherwise (crashed
  // at boot with a null host mutex assert the one time this was tried
  // first, back in Phase 2).
  NimBLEDevice::init("aigf-ble-spike");
  NimBLEDevice::setMTU(517);
  NimBLEDevice::setDefaultPhy(BLE_GAP_LE_PHY_2M_MASK, BLE_GAP_LE_PHY_2M_MASK);

  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCB());

  NimBLEService *service = server->createService(SVC_UUID);
  txChar = service->createCharacteristic(TX_CHAR_UUID, NIMBLE_PROPERTY::NOTIFY);
  txChar->setCallbacks(new TxCharCB());
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
  if (connected && (now - lastTickMs >= STATUS_INTERVAL_MS)) {
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
