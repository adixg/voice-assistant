package com.aigf.blespike

// Kotlin mirror of firmware/m5stick_bridge/src/ble_envelope.h --
// that header is the one source of truth for the wire format and every
// constant below; keep this in exact lockstep with it, not just "close
// enough." See that file's own header comment for the full design
// (direction split, the plan-doc correction, why AUDIO_CHUNK is reused for
// both mic and reply audio).

object FrameType {
    const val START: Byte = 0x01
    const val STOP: Byte = 0x02
    const val RESET: Byte = 0x03
    const val HEARD: Byte = 0x04
    const val STATUS: Byte = 0x05
    const val REPLY: Byte = 0x06
    const val AUDIO_CHUNK: Byte = 0x07
    const val END: Byte = 0x08
    const val TIME_SYNC: Byte = 0x09
    const val AUTH: Byte = 0x0A
    // Settings (both directions) and firmware updates over BLE: see
    // firmware/m5stick_bridge/src/stick_settings.h and ota_update.h.
    const val SETTINGS: Byte = 0x0B
    const val OTA_BEGIN: Byte = 0x0C
    const val OTA_DATA: Byte = 0x0D
    const val OTA_END: Byte = 0x0E
    const val OTA_STATUS: Byte = 0x0F
    // Stick -> phone, once a minute: see firmware/.../battery_report.h.
    const val BATTERY: Byte = 0x10
    // Stick -> phone: a line about what the Stick decided (wake word fired,
    // what started a turn, nothing heard), passed on as "event:" text.
    const val EVENT: Byte = 0x11
}

private const val CONT: Byte = 0xFF.toByte()
private const val MAX_PACKET = 500
private const val FIRST_HEADER = 3
private const val CONT_HEADER = 1
private const val MAX_FIRST_PAYLOAD = MAX_PACKET - FIRST_HEADER
private const val MAX_CONT_PAYLOAD = MAX_PACKET - CONT_HEADER
private const val MAX_FRAME_LEN = 4096

/**
 * Splits one logical frame into <=MAX_PACKET physical packets, handing each
 * to [send]. Stops and returns false the first time [send] does (e.g. a
 * BLE write queued but the previous one hasn't completed yet -- see
 * MainActivity's own comment on BluetoothGatt's single operation queue).
 */
fun encodeFrame(type: Byte, payload: ByteArray, send: (ByteArray) -> Boolean): Boolean {
    if (payload.size > MAX_FRAME_LEN) return false

    val firstLen = minOf(payload.size, MAX_FIRST_PAYLOAD)
    val first = ByteArray(FIRST_HEADER + firstLen)
    first[0] = type
    first[1] = (payload.size and 0xFF).toByte()
    first[2] = ((payload.size shr 8) and 0xFF).toByte()
    System.arraycopy(payload, 0, first, FIRST_HEADER, firstLen)
    if (!send(first)) return false

    var sent = firstLen
    while (sent < payload.size) {
        val chunk = minOf(payload.size - sent, MAX_CONT_PAYLOAD)
        val pkt = ByteArray(CONT_HEADER + chunk)
        pkt[0] = CONT
        System.arraycopy(payload, sent, pkt, CONT_HEADER, chunk)
        if (!send(pkt)) return false
        sent += chunk
    }
    return true
}

/**
 * Stateful reassembler, mirroring ble_envelope.h's Decoder exactly: feed()
 * physical packets in arrival order, [onFrame] fires once per fully
 * reassembled logical frame.
 */
class BleEnvelopeDecoder(private val onFrame: (type: Byte, payload: ByteArray) -> Unit) {
    private var buf = ByteArray(MAX_FRAME_LEN)
    private var have = 0
    private var want = 0
    private var type: Byte = 0
    private var active = false

    fun feed(packet: ByteArray) {
        if (packet.isEmpty()) return
        val marker = packet[0]

        if (marker == CONT) {
            if (!active) return  // stray continuation, no frame in progress -- drop
            var payloadLen = packet.size - CONT_HEADER
            if (have + payloadLen > want) payloadLen = want - have  // defensive clamp
            System.arraycopy(packet, CONT_HEADER, buf, have, payloadLen)
            have += payloadLen
        } else {
            // A new frame starting always wins, even mid-reassembly -- see
            // the C++ Decoder's identical comment for why.
            if (packet.size < FIRST_HEADER) { active = false; return }
            val wantLen = (packet[1].toInt() and 0xFF) or ((packet[2].toInt() and 0xFF) shl 8)
            if (wantLen > MAX_FRAME_LEN) { active = false; return }
            type = marker
            want = wantLen
            var payloadLen = packet.size - FIRST_HEADER
            if (payloadLen > want) payloadLen = want
            if (payloadLen > 0) System.arraycopy(packet, FIRST_HEADER, buf, 0, payloadLen)
            have = payloadLen
            active = true
        }

        if (active && have >= want) {
            onFrame(type, buf.copyOf(want))
            active = false
            have = 0
        }
    }
}
