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

## Where it stands

Phase 3 (BLE GATT protocol design — the byte-envelope codec, chunk
reassembly, bonding/auth) is next, whenever BLE work resumes. The BLE work
has lived entirely in the separate `firmware/m5stick_ble_flash_spike/` and
`android_companion/` throwaway spike projects so far — `m5stick_bridge`
itself needs no changes until Phase 4 actually merges BLE in, so normal
Wi-Fi-based firmware development can continue in the meantime with zero
interaction with this track. See `TODO.md` for the remaining phases.
