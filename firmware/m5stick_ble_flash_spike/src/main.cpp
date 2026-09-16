// Phase 2 throughput spike: one GATT service, one notify characteristic,
// streaming 1024-byte chunks (matching MIC_CHUNK_SAMPLES=512 samples * 2
// bytes in the real firmware's main.cpp:57) as fast as the tuned link
// allows. Tuning applied on connect: 2M PHY, max data length, a short
// connection interval -- the configuration the real-world benchmarks cited
// in the plan actually used, not NimBLE's untuned defaults. Range/walk-test
// dropped from this spike's scope 2026-09-14 (owner's phone stays close to
// the Stick in practice). See
// /home/aditya/.claude/plans/tranquil-drifting-stream.md, Phase 2.
#include <Arduino.h>
#include <M5Unified.h>
#include <NimBLEDevice.h>

// Freshly generated (python3 -c "import uuid; print(uuid.uuid4())"), not the
// well-known Nordic UART Service UUID this spike started with -- that one is
// reused by countless hobbyist BLE devices/apps, and something else on the
// test phone was plausibly auto-connecting to it before this app's own scan
// got there: real notify() successes kept flowing through two independent
// clean resets (Stick BLE stack reset, then a full phone Bluetooth off/on)
// while the app's own on-screen counter stayed at 0. Must match
// android_companion/app/src/main/kotlin/com/aigf/blespike/MainActivity.kt
// exactly.
static const char *SVC_UUID  = "667d22e3-b922-4852-b7a6-6a4764a26665";
static const char *CHAR_UUID = "c0819f6b-f0b7-4db8-9dcb-9f58b2745f9c";

static NimBLECharacteristic *txChar = nullptr;
static volatile bool connected = false;
static uint16_t gConnHandle = 0;

// A single BLE notification is ONE ATT PDU with no fragmentation/continuation
// mechanism -- max payload is negotiated_MTU - 3 (ATT header). At MTU 517
// that's 514 bytes. CHUNK_BYTES was 1024, which NimBLE's own
// ble_att_tx_with_conn() (ble_att_cmd.c) silently truncates via
// ble_att_truncate_to_mtu() before every send -- confirmed by reading that
// function directly. notify() still returns true (the *truncated* PDU sends
// successfully), so this never showed up as a failure on the Stick's side;
// it only meant the measured kbps never reflected what was actually
// transmitted. Capped here to a safe margin under the 514-byte ceiling.
static const size_t CHUNK_BYTES = 500;
static uint8_t chunkBuf[CHUNK_BYTES];

// Diagnostic: notify() was failing 100% of the time (~59,000 calls/sec, all
// false) with the client reporting "connected." onSubscribe fires when a
// peer writes the CCCD -- logging subValue (0=none, 1=notify, 2=indicate)
// answers directly whether Android's CCCD write ever reaches the ESP32,
// rather than guessing from notify()'s bool return alone.
class CharCB : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &info, uint16_t subValue) override {
    Serial.printf("[spike] onSubscribe: subValue=%u\n", subValue);
  }
};

class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *server, NimBLEConnInfo &info) override {
    gConnHandle = info.getConnHandle();
    Serial.printf("[spike] connected, handle=%u\n", gConnHandle);

    // Tuning, applied once per connection -- see plan Phase 2. Interval units
    // are 1.25ms (BLE spec), timeout units are 10ms: 6*1.25=7.5ms min,
    // 12*1.25=15ms max, 400*10ms=4s supervision timeout.
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
  Serial.println("[spike] Phase 2 throughput spike starting");

  for (size_t i = 0; i < CHUNK_BYTES; ++i) chunkBuf[i] = (uint8_t)(i & 0xFF);

  // setMTU()/setDefaultPhy() both call directly into the NimBLE host stack
  // (ble_att_set_preferred_mtu / ble_gap_set_prefered_default_le_phy), which
  // doesn't exist until init() has run -- calling them first crashed at boot
  // with "assert failed: npl_freertos_mutex_pend ... (mu->handle)", a null
  // host mutex. init() must come first, matching NimBLE-Arduino's own
  // examples/L2CAP/L2CAP_Server. setDefaultPhy() also takes a different
  // constant family than updatePhy() below -- the _MASK-suffixed ones.
  NimBLEDevice::init("aigf-ble-spike");
  NimBLEDevice::setMTU(517);  // request the max ATT MTU at negotiation
  NimBLEDevice::setDefaultPhy(BLE_GAP_LE_PHY_2M_MASK, BLE_GAP_LE_PHY_2M_MASK);

  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCB());

  NimBLEService *service = server->createService(SVC_UUID);
  // The READ property + polling this spike carried while root-causing the
  // notify-delivery bug (see CLAUDE.md, "Phase 2 throughput spike -- root
  // cause found") has done its job and is removed: NOTIFY-only again, now
  // that push delivery is confirmed working.
  txChar = service->createCharacteristic(CHAR_UUID, NIMBLE_PROPERTY::NOTIFY);
  txChar->setCallbacks(new CharCB());
  service->start();

  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->start();
  Serial.println("[spike] advertising");
}

// Real mic audio produces one MIC_CHUNK_SAMPLES (512 @ 16kHz = 1024 bytes)
// chunk every ~32ms -- 256 kbps. But a single notify() can carry at most
// 514 bytes (MTU-3, see CHUNK_BYTES above), so the real firmware will always
// need >=2 physical notifies per logical 1024-byte chunk (matching what the
// plan's Phase 3 protocol design already calls for: a length-prefixed
// continuation scheme). This spike models that by pacing per-*packet*, not
// per-chunk: at 500 B/packet, clearing 256 kbps needs >=64 packets/sec
// (15.6ms). 10ms gives a comfortable margin (400 kbps) without hammering the
// link at the unthrottled extreme that produced ~70,000 failed calls/sec
// earlier in this same debugging arc -- this is "comfortably clears target,"
// not "find the ceiling."
static const uint32_t NOTIFY_INTERVAL_MS = 10;

void loop() {
  static uint32_t bytesThisSecond = 0;
  static uint32_t notifiesThisSecond = 0;
  static uint32_t failuresThisSecond = 0;
  static uint32_t lastReportMs = 0;
  static uint32_t lastNotifyMs = 0;

  uint32_t nowMs = millis();
  if (connected && txChar && (nowMs - lastNotifyMs >= NOTIFY_INTERVAL_MS)) {
    lastNotifyMs = nowMs;
    // First 4 bytes carry a counter -- not read anywhere now that the READ
    // diagnostic is gone, but harmless and cheap to leave in for any future
    // serial-side eyeballing of live vs. stale data.
    static uint32_t seq = 0;
    seq++;
    memcpy(chunkBuf, &seq, sizeof(seq));
    // Counted only on success -- a false return (no subscriber yet, or the
    // stack's notify queue is full) must not inflate the measured rate.
    if (txChar->notify(chunkBuf, CHUNK_BYTES, gConnHandle)) {
      bytesThisSecond += CHUNK_BYTES;
      notifiesThisSecond++;
    } else {
      failuresThisSecond++;
    }
  } else {
    delay(1);
  }

  uint32_t now = millis();
  if (now - lastReportMs >= 1000) {
    float kbps = bytesThisSecond * 8 / 1000.0f;
    Serial.printf("[spike] %.1f kbps  (%u notifies, %u failed)\n",
                  kbps, notifiesThisSecond, failuresThisSecond);
    bytesThisSecond = 0;
    notifiesThisSecond = 0;
    failuresThisSecond = 0;
    lastReportMs = now;
  }
}
