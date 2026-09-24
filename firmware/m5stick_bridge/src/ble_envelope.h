// Phase 4: the byte-envelope codec both sides speak, copied verbatim from
// the Phase 3 spike (firmware/m5stick_ble_flash_spike/src/ble_envelope.h)
// once bonding/AUTH/TIME_SYNC/the codec were all confirmed round-tripping
// together on real hardware (2026-09-16, see docs/ble-migration.md) -- this
// is that same proven file, not a rewrite. It re-expresses
// bridge_server.py's WebSocket text/binary framing as a plain byte stream,
// since a BLE characteristic has no text-vs-binary distinction the way a
// WebSocket frame does; ble_transport.h is what actually speaks it on this
// side now, in place of the old WebSocketsClient + this file's own
// webSocketEvent()-turned-handleBleFrame().
//
// Pure logic, no BLE/Arduino dependency beyond fixed-width int types --
// deliberately kept that way so it's the same file conceptually testable
// on either side of the link (this header IS the one source of truth; the
// Kotlin mirror is android_companion/app/src/main/kotlin/com/aigf/blespike/
// BleEnvelopeCodec.kt and must match every constant and the wire format
// below exactly).
//
// Wire format, one physical BLE packet at a time (<=MAX_PACKET bytes,
// matching the measured safe margin under this link's negotiated MTU-3=514):
//
//   first packet of a logical frame:  [type:u8][totalLen:u16 LE][payload...]
//   continuation packet(s):           [CONT:u8][payload...]
//
// A receiver accumulates payload bytes across packets until it has
// totalLen of them, then dispatches (type, payload, totalLen) once.
//
// Direction split (one characteristic each way -- a BLE peripheral can only
// notify, a central can only write, so logical direction is physical
// direction here, not a free choice):
//
//   TX (Stick notifies, phone subscribes): frames the STICK originates --
//     START/STOP/RESET (button presses, ble_transport.h's sendStart()/
//     sendStop()/sendReset(), called from main.cpp's button handling) and
//     AUDIO_CHUNK (mic audio, sendAudioChunk()).
//   RX (phone writes, Stick receives): frames relayed down from
//     bridge_server.py via the phone's WebSocket client
//     (android_companion/.../RelayService.kt) -- HEARD/STATUS/REPLY/
//     AUDIO_CHUNK(reply audio)/END, plus AUTH and TIME_SYNC (neither of
//     which bridge_server.py itself sends; both are new, BLE-only, handled
//     directly in ble_transport.h rather than forwarded to main.cpp).
//
//   NOTE on the plan doc: Phase 3's RX bullet in
//   /home/aditya/.claude/plans/tranquil-drifting-stream.md lists
//   start/stop/reset under "RX (phone->Stick)", which can't be right --
//   those originate at the Stick's own buttons, and GATT has no mechanism
//   for a peripheral to receive its own button state from the central. This
//   header follows the physically-required split above instead; flagged
//   here rather than silently diverged from without a trace.
//
//   AUDIO_CHUNK is reused for both mic audio (on TX) and reply audio (on
//   RX) -- which characteristic it arrived on already disambiguates
//   purpose, so a second type isn't needed.
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace BleEnvelope {

enum FrameType : uint8_t {
  FRAME_START       = 0x01,
  FRAME_STOP        = 0x02,
  FRAME_RESET       = 0x03,
  FRAME_HEARD       = 0x04,
  FRAME_STATUS      = 0x05,
  FRAME_REPLY       = 0x06,
  FRAME_AUDIO_CHUNK = 0x07,
  FRAME_END         = 0x08,
  FRAME_TIME_SYNC   = 0x09,
  FRAME_AUTH        = 0x0A,
  // Added 2026-09-24 (stick_settings.h / ota_update.h). SETTINGS goes both
  // ways: the phone sends new values, the Stick reports what it applied (and
  // its firmware version) after AUTH and after every change. The OTA_* frames
  // carry a firmware update from the phone; OTA_STATUS is the Stick's answer.
  FRAME_SETTINGS    = 0x0B,
  FRAME_OTA_BEGIN   = 0x0C,
  FRAME_OTA_DATA    = 0x0D,
  FRAME_OTA_END     = 0x0E,
  FRAME_OTA_STATUS  = 0x0F,
};

constexpr uint8_t CONT = 0xFF;  // continuation marker -- never a real logical frame type

// 500, not the full MTU-3=514: matches CHUNK_BYTES's already-measured safe
// margin in this spike's main.cpp (notify() silently truncates anything
// over MTU-3, so this stays comfortably under that rather than riding the
// exact edge).
constexpr size_t MAX_PACKET = 500;
constexpr size_t FIRST_HEADER = 3;  // type(1) + totalLen(2)
constexpr size_t CONT_HEADER = 1;   // marker(1) == CONT
constexpr size_t MAX_FIRST_PAYLOAD = MAX_PACKET - FIRST_HEADER;
constexpr size_t MAX_CONT_PAYLOAD = MAX_PACKET - CONT_HEADER;

// Real frames today (transcript/caption text, one mic/reply audio chunk)
// are at most a few hundred bytes; 4096 gives headroom to prove multi-
// packet reassembly actually works without reserving an oversized static
// buffer on a RAM-constrained target. Bump this later if Phase 4 finds a
// real frame that needs more.
constexpr size_t MAX_FRAME_LEN = 4096;

// Splits one logical frame into <=MAX_PACKET physical packets, handing each
// to `sink`. Stops and returns false the first time `sink.send()` does
// (e.g. notify() failed because no one's subscribed) -- a caller then knows
// to retry the whole logical frame rather than leaving a partial one on the
// wire for the decoder to choke on.
class PacketSink {
 public:
  virtual bool send(const uint8_t *packet, size_t len) = 0;
  virtual ~PacketSink() = default;
};

inline bool encode(uint8_t type, const uint8_t *payload, size_t len, PacketSink &sink) {
  if (len > MAX_FRAME_LEN) return false;

  uint8_t pkt[MAX_PACKET];
  size_t firstLen = len < MAX_FIRST_PAYLOAD ? len : MAX_FIRST_PAYLOAD;
  pkt[0] = type;
  pkt[1] = (uint8_t)(len & 0xFF);
  pkt[2] = (uint8_t)((len >> 8) & 0xFF);
  if (firstLen) memcpy(pkt + FIRST_HEADER, payload, firstLen);
  if (!sink.send(pkt, FIRST_HEADER + firstLen)) return false;

  size_t sent = firstLen;
  while (sent < len) {
    size_t chunk = (len - sent) < MAX_CONT_PAYLOAD ? (len - sent) : MAX_CONT_PAYLOAD;
    pkt[0] = CONT;
    memcpy(pkt + CONT_HEADER, payload + sent, chunk);
    if (!sink.send(pkt, CONT_HEADER + chunk)) return false;
    sent += chunk;
  }
  return true;
}

// Stateful reassembler: feed() physical packets in arrival order (BLE
// delivers in order on one characteristic -- one ATT connection, not
// multiple racing streams -- so an out-of-sequence marker can only mean a
// dropped packet or a fresh frame starting, never reordering). Calls
// onFrame() exactly once per fully-reassembled logical frame.
class FrameSink {
 public:
  virtual void onFrame(uint8_t type, const uint8_t *payload, size_t len) = 0;
  virtual ~FrameSink() = default;
};

class Decoder {
 public:
  explicit Decoder(FrameSink &sink) : sink_(sink) {}

  void feed(const uint8_t *packet, size_t len) {
    if (len == 0) return;
    uint8_t marker = packet[0];

    if (marker == CONT) {
      if (!active_) return;  // stray continuation, no frame in progress -- drop
      size_t payloadLen = len - CONT_HEADER;
      if (have_ + payloadLen > want_) payloadLen = want_ - have_;  // defensive clamp
      memcpy(buf_ + have_, packet + CONT_HEADER, payloadLen);
      have_ += payloadLen;
    } else {
      // A new logical frame starting always wins, even mid-reassembly of a
      // previous one -- that previous one lost a continuation packet
      // somewhere and can't be completed correctly anyway.
      if (len < FIRST_HEADER) { active_ = false; return; }  // malformed, drop
      uint16_t wantLen = (uint16_t)packet[1] | ((uint16_t)packet[2] << 8);
      if (wantLen > MAX_FRAME_LEN) { active_ = false; return; }  // refuse oversized
      type_ = marker;
      want_ = wantLen;
      size_t payloadLen = len - FIRST_HEADER;
      if (payloadLen > want_) payloadLen = want_;
      if (payloadLen) memcpy(buf_, packet + FIRST_HEADER, payloadLen);
      have_ = payloadLen;
      active_ = true;
    }

    if (active_ && have_ >= want_) {
      sink_.onFrame(type_, buf_, want_);
      active_ = false;
      have_ = 0;
    }
  }

 private:
  FrameSink &sink_;
  uint8_t buf_[MAX_FRAME_LEN];
  size_t have_ = 0;
  size_t want_ = 0;
  uint8_t type_ = 0;
  bool active_ = false;
};

}  // namespace BleEnvelope
