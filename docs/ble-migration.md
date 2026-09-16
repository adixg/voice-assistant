# BLE transport migration — full log

Detail behind the current-state summary in `CLAUDE.md`. Full plan:
`/home/aditya/.claude/plans/tranquil-drifting-stream.md`.

Motivation: replace the phone's persistent Wi-Fi hotspot (battery/data drain)
with BLE — ESP32-S3 is BLE-only (no Classic BT, confirmed against
Espressif's ESP-IDF docs), so this is a from-scratch GATT protocol + a new
Android companion app, not a Wi-Fi-Direct-style swap (Wi-Fi Direct was
checked and ruled out too: ESP-IDF has never implemented the P2P negotiation
it needs — open upstream issue `espressif/esp-idf#6522`).

## Phase 1 — flash-budget spike PASSES (2026-09-14)

Spike lives in `firmware/m5stick_ble_flash_spike/` (throwaway, same pattern as
`m5stick_echo_test/` etc. — one `platformio.ini` + `src/main.cpp`, `lib_deps`
edited in place between runs, not four separate directories). Four configs
measured against the real `m5stick_bridge` baseline of **2,647,008 B (84.1%)**:

| config | lib_deps | flash used |
| --- | --- | ---: |
| A | NimBLE-Arduino alone | 503,317 B (16.0%) |
| A2 | M5Unified alone | 507,513 B (16.1%) |
| B | NimBLE + M5Unified | 730,149 B (23.2%) |
| C | NimBLE + M5Unified + WebSockets + WiFi.h | 1,105,989 B (35.2%) |
| D | M5Unified + WebSockets + WiFi.h (today's real lib baseline) | 923,585 B (29.4%) |

From these: **NimBLE-Arduino's true marginal cost is 222,636 B (7.1 points)** —
`B - A2`, not the cruder `A` vs `B` eyeball. Isolating the real firmware's own
application code (`real_bridge - D`) gives **1,723,423 B**, which lets both
end states be projected rigorously rather than guessed:

| scenario | projected flash | headroom |
| --- | ---: | ---: |
| **End state** — BLE only, Wi-Fi/WebSockets fully removed (`app_code + B`) | **78.0%** | **22.0% (692,156 B)** |
| **Transitional** — BLE *and* Wi-Fi both kept for a bring-up safety net (`app_code + C`) | **89.9%** | 10.1% (316,316 B) |

**Verdict: Phase 1 passes clearly for the plan's actual design (full
replacement).** 78% with 22% headroom is comfortable room for Phase 3's
envelope codec, chunk reassembly, and bonding/auth logic — none of which
exists yet.

**The transitional/coexistence option is technically possible but not
recommended.** 89.9% sits right at the plan's own "~10% headroom" caution
threshold — that 10.1% has to cover the same not-yet-written protocol/auth
code, leaving very little margin for anything else (library version bumps,
stack/heap safety margin). Recommendation: skip the "keep Wi-Fi as a bring-up
safety net" option Phase 6 offered conditionally — validate BLE via this
disposable spike firmware instead (as Phase 6 already allows either way), and
make Wi-Fi removal part of the same change that adds BLE to `m5stick_bridge`,
not a deferred follow-up.

No PlatformIO/NimBLE build friction hit (the known issue `h2zero/NimBLE-Arduino#946`
did not reproduce here). All four configs built clean on the first try.

**Range/walk-test dropped from Phase 2 and Phase 6, 2026-09-14** — owner's
call: the phone will always be physically close to the Stick in real use, so
it isn't a real constraint here. Plan file updated to match.

## Phase 2 — throughput spike, full debugging arc

### Two real bugs found and fixed, before it ever ran

Extends the same spike directory (`firmware/m5stick_ble_flash_spike/`), now
on lib_deps "config B" (NimBLE + M5Unified). One GATT service, one notify
characteristic streaming 1024-byte chunks (matching `MIC_CHUNK_SAMPLES = 512`
samples × 2 bytes in the real firmware). First flash **crash-looped at boot**:

    assert failed: npl_freertos_mutex_pend npl_os_freertos.c:265 (mu->handle)

Traced by reading NimBLE-Arduino's own source (`v2.5.1`, fetched during Phase
1) rather than guessing:

1. **`NimBLEDevice::setMTU()` and `setDefaultPhy()` were called *before*
   `NimBLEDevice::init()`.** Both call straight into the NimBLE host stack
   (`ble_att_set_preferred_mtu` / `ble_gap_set_prefered_default_le_phy`),
   which doesn't exist until `init()` has run — hence the null host mutex.
   The library's own bundled example
   (`examples/L2CAP/L2CAP_Server/L2CAP_Server.ino`) calls `setMTU()`
   *after* `init()`; nothing in the docs states this ordering requirement
   explicitly, the working example is the only place it's evident.
2. **`setDefaultPhy()` needs a different constant family than
   `NimBLEServer::updatePhy()`.** `updatePhy()` (per-connection, called from
   `onConnect`) takes plain `BLE_GAP_LE_PHY_2M`; `setDefaultPhy()` (global
   preference, called once at startup) takes the `_MASK`-suffixed
   `BLE_GAP_LE_PHY_2M_MASK` — both compile fine with either, so this doesn't
   surface as a build error, only as silently-wrong behaviour.

Fixed: `init()` first, then `setMTU()`/`setDefaultPhy()`. Reflashed — boots
clean, advertises, `0.0 kbps (0 notifies, 0 failed)` on the serial monitor
with nothing connected yet, as expected.

### Android tooling — set up 2026-09-14 (WSL2, CLI-only, no Android Studio)

Sudo-free where possible: JDK 17 installed in a dedicated conda env
(`conda create -n android-dev -c conda-forge openjdk=17`) rather than
`apt install openjdk-17-jdk`, since `sudo` here needs an interactive password
this session can't supply. Android SDK cmdline-tools installed to
`~/Android/sdk` by scraping the current build number off
`developer.android.com/studio` rather than hardcoding one (they change every
release) — landed on `commandlinetools-linux-15859902_latest.zip`.
`platform-tools` (adb) 37.0.1, `platforms;android-34`, `build-tools;34.0.0`
via `sdkmanager`. No standalone `gradle` needed — a real Gradle project's
checked-in `gradlew` wrapper self-downloads the pinned version.

**Chose CLI-only over Android Studio deliberately**: the Android emulator
cannot meaningfully test BLE (confirmed — Google's own workarounds only cover
emulated audio-output-switching via the Bumble project, not real GATT
peripheral connections), so this project needs a real phone for testing
either way, which removes the emulator's main selling point. `android_companion/`
is mostly a foreground service + BLE glue + a minimal settings screen — not
layout-editor territory.

**Real device connected over USB** (adb over Wi-Fi was tried first and
abandoned — the "IP address & Port" on the Wireless-debugging screen changes
on every reconnect, more fiddly than USB for a machine that's staying put).
Same `usbipd-win` flow already used for the Stick, but the phone needed two
things the Stick didn't:

1. **`usbipd bind --busid <id>` once, from an elevated PowerShell**, before
   `attach` will work at all — the Stick's serial adapter had apparently
   already been bound in an earlier session; the phone hadn't.
2. **A udev rule**, because raw `adb` USB access needs read+write on
   `/dev/bus/usb/<bus>/<dev>`, unlike the Stick's CDC-ACM path which lands in
   the `dialout` group automatically. Without it: `adb devices` shows the
   device but `no permissions (missing udev rules? user is in the plugdev
   group)` — being in `plugdev` doesn't help if the node's *group* is `root`,
   which it was. Fixed with a one-line rule for Samsung's vendor ID:

       echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="04e8", MODE="0666", GROUP="plugdev"' \
           | sudo tee /etc/udev/rules.d/51-android.rules
       sudo udevadm control --reload-rules && sudo udevadm trigger

   `udevadm trigger` re-processed the already-attached device node in place —
   no replug or re-attach needed. (WSL2 here runs a real `systemd-udevd`;
   this wouldn't apply to a WSL setup without systemd enabled.)

Confirmed working: `adb devices` shows `RZCY21G04RR device`, phone is a
**Samsung Galaxy A36 (SM-A366E), Android 16, SDK 36**.

**On this machine, only one of {Stick, phone} could reliably be on USB at a
time for most of this debugging arc** (single free port for long stretches) —
Bluetooth itself doesn't need either device tethered to the dev machine, only
flashing/adb access does, so most tests were: flash/attach one device, detach,
attach the other, observe. A second free port (used later in the session)
removes this constraint entirely.

### Root cause found, notify delivery CONFIRMED (2026-09-15)

The multi-day mystery — the Stick's own serial log always reported successful
`notify()` calls, but the Android app's `onCharacteristicChanged` never fired,
counter stuck at 0.0 kbps — is **solved and fixed**. Root cause, confirmed by
reading NimBLE-Arduino's own source rather than guessing:

**A single BLE notification is one ATT PDU with no fragmentation or
continuation mechanism.** Max payload is `negotiated_MTU - 3` (ATT opcode +
handle); at MTU 517 that's **514 bytes**. The spike's `CHUNK_BYTES` was
**1024** — every `notify()` call was silently truncated by NimBLE's own
`ble_att_tx_with_conn()` (`ble_att_cmd.c`), which calls
`ble_att_truncate_to_mtu(chan, txom)` immediately before every send.
`notify()` still returned `true` (the *truncated* PDU sent successfully), so
this never surfaced as a failure on the firmware side — it just meant the
measured kbps never reflected what was actually transmitted, and worse, the
truncated/malformed packet was evidently bad enough that Android's Bluetooth
stack discarded it outright rather than delivering a short notification —
explaining the total silence on the Android side with zero visible error on
either end. **Fix: `CHUNK_BYTES` capped at 500** (safely under the 514-byte
ceiling). Confirmed via real `Log.d`-based logcat capture (the on-screen
TextView log is not visible to `adb logcat` at all — `log()` in
`MainActivity.kt` only calls `TextView.append()`, never `android.util.Log`;
real diagnosis needed explicit `Log.d` calls added to
`onCharacteristicChanged`/`onNotification`):

    onCharacteristicChanged(3-arg) fired, 500 bytes
    onNotification: 500 bytes
    (repeated, matching the Stick's send rate)

This was the last of several **real, distinct bugs** found and fixed in this
same debugging arc, each confirmed by reading NimBLE-Arduino/AOSP source
rather than guessing — worth keeping the full chain here since re-deriving
any one of them from scratch would cost real time again:

1. **GATT operation queue clobbering** (`MainActivity.kt`): the Android app
   fired `requestConnectionPriority` + `setPreferredPhy` + `requestMtu` back
   to back in `onConnectionStateChange`, before any of their completion
   callbacks landed. `BluetoothGatt` has a single internal operation queue;
   firing multiple ops before the previous one's callback fires silently
   drops the later ones. Fixed by chaining each step off the previous one's
   real callback (`onConnectionStateChange` → `setPreferredPhy` only →
   `onPhyUpdate` → `requestMtu` → `onMtuChanged` → `discoverServices`).
2. **Deprecated `writeDescriptor(descriptor)` + `.value=` setter, API 33+**:
   Google's own docs deprecate this pair as "not memory safe," and its return
   value (a plain `Boolean`, meaning "was this even queued") was never
   checked — a `false` return meant `onDescriptorWrite` legitimately never
   fires, indistinguishable from a slow pending write. Fixed with the new
   `writeDescriptor(descriptor, value): Int` (SDK ≥33) plus always logging
   the real result/status.
3. **ATT error 17 (`BLE_ATT_ERR_INSUFFICIENT_RES`) on the CCCD write itself**,
   reproduced twice. Traced through `ble_gatts.c`: the CCCD-write handler
   only returns this on a *read*, and only persists to the CCCD store for
   *bonded* connections (this spike never bonds) — ruling out a
   store-capacity theory. The remaining sites are all in the ATT server's
   mbuf allocator (`ble_att_svr.c`), i.e. the `msys_1` pool — NimBLE's own
   `nimconfig.h` comment: "may need to be increased if you are sending large
   blocks of data." A 517-byte MTU plus several GATT ops landing within ~1s
   of connect plausibly exhausts the default 12-block pool. Fixed:
   `-DCONFIG_BT_NIMBLE_MSYS1_BLOCK_COUNT=24` in `platformio.ini`.
4. **`NimBLEAttValue`'s silent 512-byte cap**: `setValue(chunkBuf, 1024)` —
   added as a read-diagnostic fallback — silently no-op'd on every call.
   `NimBLEAttValue::append()` (`NimBLEAttValue.cpp`) checks
   `len > m_attr_max_len` (default `BLE_ATT_ATTR_MAX_LEN` = 512) and just
   logs+returns, and `setValue()` resets the stored length to 0 *before*
   calling `append()` — so a rejected call leaves the stored value
   permanently empty rather than unchanged. This only affected the
   diagnostic READ path (`notify()` sends its buffer argument directly, never
   touching this stored value) but was the second oversized-payload bug in
   the same session, from the same underlying habit (treating `CHUNK_BYTES`
   as fine to reuse everywhere without checking against a real BLE ceiling).
5. **Zombie connections from `am force-stop`**: used throughout this session
   to get "clean" test runs, `force-stop` kills the process without running
   `onDestroy()`, so `gatt.close()` never fires — the connection survives at
   the Android Bluetooth *system* level even after the app process is dead,
   and kept streaming to nobody. Confirmed by `adb shell svc bluetooth
   disable`/`enable` (a full radio-level reset), which killed it instantly.
   **Lesson: never use `force-stop` to reset a BLE test app state truthfully
   — either let it disconnect cleanly or toggle Bluetooth itself.**

**Diagnostic technique that actually cracked it**: adding a `READ` property
to the characteristic (alongside `NOTIFY`) as a fallback the Android app
polls once a second, carrying a live incrementing counter. Once reads came
back with real, correctly-changing values (`seq=270, 359, 422...`) while
notify delivery stayed at zero, that *proved* the connection, subscribe, and
GATT server were all genuinely healthy — narrowing the remaining mystery from
"something about this connection" to "specifically push notifications, nothing
else," which pointed straight at a PDU-level framing difference between reads
(has a Read Blob continuation mechanism) and notifications (none) — and from
there directly to the oversized-payload theory.

**The read-polling connection drop (`Disconnected status=8`) was indeed just
the diagnostic contending with notify traffic** — confirmed by its absence
once removed. Not a real Phase 2 concern.

## Phase 2 CLOSED — PASS (2026-09-15)

Removed the READ/poll diagnostic (job done) and fixed `NOTIFY_INTERVAL_MS`
for the new MTU-safe `CHUNK_BYTES=500` (a 1024-byte logical chunk now needs
≥2 physical notifies — same continuation scheme the plan's Phase 3 protocol
design already calls for — so pacing per-*packet* at 10ms clears 256 kbps
with margin rather than accidentally halving it). Final measurement, counted
on the **Android side** via the on-screen kbps readout (not just the Stick's
serial log), sustained over 25+ consecutive one-second samples with zero
drops or gaps:

    396-408 kbps, ~100 notifies/sec, ~50,000 bytes/sec

Matches the Stick's own serial measurement (400.0 kbps, 100 notifies, 0
failed) almost exactly. **Comfortably clears the ~256 kbps target with ~56%
headroom**, genuinely received and counted end-to-end, not inferred from the
sender. Phase 2's go/no-go: **pass**.

## Phase 3 — byte-envelope codec, in progress (2026-09-16)

Codec + two-characteristic split written and build-checked on both sides
(not yet flashed/run on real hardware as of this commit). Design: `firmware/
m5stick_ble_flash_spike/src/ble_envelope.h` is the one source of truth for
the wire format (1-byte type + 2-byte length on the first packet of a
logical frame, `0xFF`-marked continuation packets after it, capped at the
already-measured 500-byte safe margin under this link's negotiated
MTU-3=514), mirrored exactly in `android_companion/.../BleEnvelopeCodec.kt`.

**A correction to this plan document while implementing it**: Phase 3's own
RX bullet above lists `start`/`stop`/`reset` control frames under "RX
(phone→Stick)" — that can't be right, since those originate at the Stick's
own physical buttons, and GATT gives a peripheral no way to receive its own
button state from the central. Implemented instead with the only direction
split that's physically possible: **TX (Stick notifies)** carries
START/STOP/RESET (button-triggered) and AUDIO_CHUNK (mic audio, matching
today's real `webSocket.sendTXT("start")`/`sendBIN(...)` calls at
`main.cpp:941/963/907`); **RX (phone writes)** carries HEARD/STATUS/REPLY/
AUDIO_CHUNK(reply audio)/END relayed down from `bridge_server.py`, plus AUTH
and TIME_SYNC (neither exists in the Wi-Fi protocol; both are new,
BLE-only). `AUDIO_CHUNK` is reused for both mic and reply audio — which
characteristic it arrived on already disambiguates purpose.

The spike firmware (`m5stick_ble_flash_spike`) now runs a real bidirectional
demo: periodic STATUS frames over TX (one short, one deliberately padded
past 497 bytes every 25th tick to force the continuation path, not just the
trivial single-packet case), and an `RxLogSink` that decodes and logs
whatever the phone writes to RX. Builds clean: **23.3% flash, 11.4% RAM**
(comfortably inside Phase 1's projected 78.0%-flash end state — this is
still the flash-spike project, not the real firmware). The Android side
compiles clean too (`./gradlew compileDebugKotlin`, conda env
`android-dev`) and now writes a test HEARD frame 3 seconds after
subscribing, using the same single-operation-queue discipline (chain off
each op's real completion callback) that Phase 2's debugging arc already
established is required on `BluetoothGatt`.

**Round-trip CONFIRMED on real hardware (2026-09-16).** Both sides flashed
and run; captured via the Stick's serial log and the phone's `adb logcat`
simultaneously, not inferred from one side alone:

- **TX (Stick→phone)**: STATUS frames decoded correctly and continuously
  (`decoded frame type=0x05 len=13: alive tick=N` every ~200ms), including
  the padded frame every 25th tick reassembling correctly from two physical
  packets — `onCharacteristicChanged` fired once for 500 bytes then once for
  54 bytes, decoded as one `len=550` logical frame. Proves the continuation
  path works on real hardware, not just the trivial single-packet case.
- **RX (phone→Stick)**: the Stick's serial log showed
  `[spike] RX frame type=0x04 len=26: test transcript from phone` a few
  seconds after subscribing — the phone's write, chained through
  `BluetoothGatt`'s single-operation queue as designed, reassembled and
  decoded correctly on the firmware side.

## Phase 3 CLOSED — bonding, AUTH, TIME_SYNC all confirmed (2026-09-16)

Bonding (`NimBLEDevice::setSecurityAuth(true, false, true)`, Just Works —
see `main.cpp`'s header comment for why no MITM protection), the app-layer
shared-secret AUTH handshake, and TIME_SYNC are all implemented and
confirmed round-tripping on real hardware, captured on the Stick's serial
log:

    [spike] pairing complete: bonded=1 encrypted=1 authenticated=0
    [spike] onSubscribe: subValue=1
    [spike] MTU negotiated: 517
    [spike] AUTH frame: accepted
    [spike] TIME_SYNC: epoch=1789570999
    [spike] RX frame type=0x04 len=26: test transcript from phone

and on the phone's on-screen log: STATUS frames (including another padded,
multi-packet one) resumed flowing immediately once `appAuthed` flipped
true, confirming the "nothing streams pre-auth" gate works in both
directions, not just that AUTH itself was accepted.

Getting there took three real, reproducible bugs, each found and fixed
against actual hardware rather than assumed away:

1. **RX's bond-state `BroadcastReceiver` never fired with
   `RECEIVER_NOT_EXPORTED`.** That's the generally-recommended flag for a
   system-only protected broadcast like `ACTION_BOND_STATE_CHANGED`, and it
   compiled/ran without error — but `adb shell dumpsys activity broadcasts`
   showed zero delivery history for the app's receiver even after the
   Bluetooth stack's own logs confirmed `BOND_BONDING -> BOND_BONDED`
   completed. Switched to `RECEIVER_EXPORTED`; not fully root-caused (may be
   this specific Samsung build), but verified working, not assumed.
2. **`NIMBLE_PROPERTY::WRITE_ENC` on the RX characteristic made every write
   fail with `ERROR_GATT_WRITE_NOT_ALLOWED` (200)**, reproducibly, even with
   bonding+encryption genuinely complete on the Stick's own side first, and
   even after 5 retries with a 300ms backoff (ruling out a transient
   encryption-settling race — a real retry would have cleared it). Root
   cause not fully isolated; fixed by dropping to plain `WRITE` and relying
   on the AUTH frame as the actual access-control gate instead, which is
   what it was already doing the real work of regardless of the ATT layer's
   own encryption requirement. Full reasoning in `main.cpp`'s header
   comment.
3. **A stale bond after repeated reflashing.** Once bonding started failing
   consistently (`bonded=0` on 4/4 attempts, disconnect reason 19) after an
   otherwise-unrelated firmware change, the cause was the phone holding an
   old bond record that no longer matched the Stick's actual keys. No clean
   adb-only single-device unpair exists (only a full Bluetooth
   `factoryReset`, which would also drop unrelated paired devices like
   headphones — not something to do without being asked). Fixed with a
   manual Forget/unpair on the phone's Bluetooth settings, then a clean
   re-pair succeeded first try.

`ble_envelope.h`'s header comment carries the final security-model writeup
and is the source of truth going forward; this log is the story of getting
there.

## Phase 5 — the real Android companion app, DONE (2026-09-16)

Built out of order relative to Phase 4 (deliberately — nothing about the
Android app's WebSocket-bridge half depends on the Stick running real
firmware yet), `android_companion/` is no longer just the Phase 2/3
throughput/protocol spike. Three new files:

- **`RelayService.kt`** — a foreground `Service`, not tied to
  `MainActivity`'s lifecycle, owning both the BLE central connection (moved
  here verbatim from the proven Phase 3 code) and a new OkHttp `WebSocket`
  client to `bridge_server.py`. This is the actual protocol translation
  layer `tools/termux_relay.py` never had to do (that script just pumps
  bytes between two WebSocket endpoints, since both sides already spoke
  WebSocket) — here the Stick side is BLE, so every frame crosses formats:
  `START`/`STOP`/`RESET`/`AUDIO_CHUNK` TX frames become `bridge_server.py`'s
  `start`/`stop`/`reset` text and binary mic chunks; its
  `heard:`/`status:`/`reply:`/binary-audio/`end` messages become
  `HEARD`/`STATUS`/`REPLY`/`AUDIO_CHUNK`/`END` RX frames back down to the
  Stick. TIME_SYNC re-sends every 5 minutes while connected. Auto-reconnect
  on both the BLE side (already existed) and the WebSocket side (new,
  backoff retry) — losing either one tears down and reopens both, so
  `bridge_server.py` never accumulates state for a Stick that's no longer
  actually reachable over BLE.
- **`Prefs.kt`** — the "laptop host selection, shared-secret entry" the plan
  called for, as a small `SharedPreferences` wrapper.
- **`MainActivity.kt`** — rewritten from owning the BLE connection directly
  to a thin settings/control UI: host + secret fields, Start/Stop (which
  starts/stops `RelayService`), a "Forget device" shortcut (opens system
  Bluetooth settings — no non-hidden-API way to unpair a specific device
  exists, and `docs/ble-migration.md`'s own Phase 3 log already needed a
  manual unpair once), and a battery-optimization exemption request.

**Confirmed on real hardware**, not just build-checked: full sequence —
scan, connect, (already-)bonded, subscribe, AUTH accepted, then **WS
connected to bridge_server.py** — logged by the app, and independently
confirmed via `ss -tn state established` on the laptop showing the real TCP
connection to `tools/echo_server.py` (standing in for `bridge_server.py`
per its own docstring, exactly what Phase 6 already called for validating
against first). STATUS frames from the Stick logged as "unexpected TX frame
type=0x05" during this test — correctly so: the Stick is still running
`m5stick_ble_flash_spike`'s synthetic demo payload, not real firmware, so
this is the *expected* signature of Phase 4 not being done yet, not a bug.

Two real bugs found and fixed getting here:

1. **Android blocks cleartext traffic by default since API 28.**
   `bridge_server.py` only ever speaks plain `ws://` (see
   `tools/termux_relay.py`, `secrets.h.example`), so the WebSocket failed
   immediately with `CLEARTEXT communication ... not permitted by network
   security policy`. Fixed with `android:usesCleartextTraffic="true"`,
   deliberately not scoped to one host (the bridge host is a user-entered
   setting, not a fixed domain) — this traffic never leaves a private
   Tailscale/LAN network to begin with, the same threat model the BLE side's
   own AUTH-secret-over-Just-Works design already assumes.
2. **`targetSdk` 36 (Android 15+) enforces edge-to-edge layout
   unconditionally.** The host field's hint rendered half behind the status
   bar; `WindowCompat.setDecorFitsSystemWindows(window, true)` — the
   pre-Android-15 way to opt out — had zero effect, matching Android 15+
   making that call a no-op for apps targeting SDK 35+. Fixed properly with
   a `ViewCompat.setOnApplyWindowInsetsListener` applying the real system-bar
   insets as padding on the root view.

## Where it stands

Phases 1, 2, 3, and 5 are done. Phase 4 (merging BLE into the real
`firmware/m5stick_bridge/`) is the remaining blocker before a genuine
end-to-end test is possible — `android_companion/` is ready and waiting on
the other end. `m5stick_bridge` itself still needs no changes until Phase 4
actually starts, so normal Wi-Fi-based firmware development can continue in
the meantime with zero interaction with this track. See `TODO.md` for the
remaining phases.
