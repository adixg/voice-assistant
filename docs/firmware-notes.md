# Firmware notes — bug fixes, event loop, clock/pomodoro screens

Detail behind the current-state summary in `CLAUDE.md`. Covers
`firmware/m5stick_bridge/`.

## Firmware — flashed 2026-09-07

All three of the changes below were flashed to the Stick 2026-09-07
(`pio run -t upload --upload-port /dev/ttyACM0`; that flash was 2638336 bytes,
83.9% of 3145728, RAM 15.8%).

**Flashing from WSL2** — the Stick is not visible to WSL by default. It has to
be handed over from Windows first via `usbipd attach --wsl --busid <id>`
(check the current busid with `usbipd list` — it moves between ports), which
makes it `/dev/ttyACM0` (or `ttyACM1`+ if something else is already
attached). Release it back to Windows with `usbipd detach --busid <id>`.
Without this step `pio run -t upload` fails with no port found, and there is
no `/dev/ttyUSB*` to look for — it enumerates as CDC-ACM, not as a
USB-serial bridge.

1. **`applyMicConfig()` — reverted to a single boot-time call.** It was
   briefly re-applied on every `M5.Mic.begin()`; that re-assert **changed
   nothing and was removed** (flashed 2026-09-07, 2638320 bytes, hash
   verified). The call in `setup()` stays, because the board's defaults are
   not 16 kHz mono. Verified 2026-09-07 by reading `M5Unified/src/utility/Mic_Class.{hpp,cpp}`
   at the pinned version in `.pio/libdeps/`:

   - `mic_config_t config() const` returns a *copy* of the member `_cfg`, and
     `config(cfg)` just assigns it back. Re-applying the values already there
     is a plain self-assignment.
   - `Mic_Class::end()` stops the task, clears `_rec_info[]` and calls
     `_i2s_driver_uninstall(_cfg.i2s_port)`. **It never touches `_cfg`.**
     `_cfg` lives as long as the `M5.Mic` object, so it survives every
     `end()`/`begin()` cycle intact, and `begin()` reinstalls the I2S driver
     from those same retained values.
   - The mic and speaker hold *separate* config structs, so a speaker cycle
     cannot clobber the mic's.
   - `sample_rate` wasn't load-bearing anyway: the recording call passes it
     explicitly as `M5.Mic.record(micBuf, MIC_CHUNK_SAMPLES, SAMPLE_RATE)`.

   So the "different mic state after a reply" theory was wrong. The
   enrollment bug (`docs/voice-pipeline.md`) was fixed entirely by
   re-enrolling with playback between the samples, and the rejections logged
   before this flash turned out to be other people, not a mic-state problem —
   confirmed by the owner.
2. **The `status:` branch in `webSocketEvent`** — updates only the caption
   and leaves the state at THINKING, so the mauve particle field keeps
   running while an agent runs a tool. Unknown text messages were already
   ignored, so the server can send `status:` safely today; it just isn't
   displayed unless the firmware is flashed with this branch.
3. **The `mouth_closed` background fix** (sprite regeneration) — see below.

### `mouth_closed` was the only sprite with a near-black background

Diagnosed 2026-09-07, after a first attempt blamed the amplitude sampling and
changed nothing. The real cause was in the sprite sheet extraction.

The crop window is hung off her body at `WIDTH_ZOOM` (2.1), and
**`mouth_closed` is the first cell of its v2 row** — close enough to the
sheet's left edge that its window runs past it. Measured: `x0 = -8`, and it
is the *only* one of the 19 sprites that overhangs.

Two failures then compounded in `normalised_crop()`:

1. `sheet.crop((-8, ...))` — PIL pads out-of-bounds with **pure black**,
   giving an 8px black column at local x 0-7.
2. `bright[y0:y1, -8:217]` — numpy reads `-8` as *8 from the right edge*, so
   the slice is `1528:217`, i.e. **empty**. `mask.shape` was then `(120, 0)`.

The guard `if mask.shape == (crop.size[1], crop.size[0])` turned both into one
silent failure: shapes disagreed, so **the entire CANVAS_BG recolouring was
skipped** for that sprite and the raw v2 sheet background survived. Measured
before the fix:

| sprite | exact `#24273a` px | dark (≤45) px |
| --- | ---: | ---: |
| `mouth_closed` | 2860 | **16008** |
| `mouth_small` | 18764 | 0 |
| `listening` | 17922 | 0 |

`mouth_closed` is what `characterSpriteFor()` returns when `speakLevel <=
0.08` — i.e. **during every silence while she speaks**. The sprite is 220px
wide on a 240px screen, so its background sits behind the waveform bars, and
a near-black box appearing behind them on each pause is what read as the bars
"breaking".

The fix replaces the slice-and-guard with `window_mask()`, which builds a mask
the size of the window and treats anything off-sheet as background.

### …and it dragged the sheet's own border in with it

Second finding, same crop. With the background fixed, a **1px vertical stripe
at canvas x=31** was still there, running the full height of the waveform
band. `mouth_closed` was the only sprite with any non-background pixel in that
region at all — 45 of them, all in one column.

It is the **v2 sheet's printed left border at sheet x=13**, colour ≈`#6a7083`.
It is bright, so `CONTENT_THRESHOLD` keeps it and the crop preserves it as if
it were art. Harmless for every crop that sits inside the sheet; `mouth_closed`
is the only one that overhangs far enough to reach it.

Canvas x=31 falls in the 2px gap between the bars at x25-30 and x33-38 — the
3rd and 4th from the right of the left group — which is exactly where the
break was reported.

`frame_lines()` now removes any column or row that is bright across
≥`FRAME_LINE_FRACTION` (0.8) of the sheet. Measured borders: v2 x=13 at 0.82,
v3 x=20-21 at 0.86, idle x=16-17 at 0.81, plus each sheet's right-hand
counterpart and v3's horizontal row rules at 0.95-0.97. Real art tops out
around 0.2 on this measure — a cell is about a sixth of a sheet's height — so
the threshold has a wide margin.

Across both fixes, **only `mouth_closed.png` changed**; the other 42 PNGs are
byte-identical. Regression tests in `tests/test_make_face_sprites.py`.

**The earlier amplitude-sampling theory was wrong and has been reverted.** It
explained why *speaking* differs from *listening*, but not why the artifact
appeared as a dark region rather than a short bar, and the fix changed nothing
on the device. The 20ms-window-every-40ms sampling is still there, unchanged.

### Superseded: the speaking waveform gap

Diagnosed 2026-09-07 from a report that the bar column breaks while she
speaks. Both columns read the same `levelHist` ring buffer at mirrored x
(`canvas.width() - x - barW`), so the gap was on both sides — it is only
visible on whichever one you happen to watch.

The two states fed that buffer differently:

- **Listening** RMSes every `MIC_CHUNK_SAMPLES` (512 = 32ms) chunk as it
  arrives — contiguous, 100% of the audio measured.
- **Speaking** took a **20ms window once every 40ms**, positioned by wall
  clock. *Half the audio was never measured.* When that window landed in the
  gap between two words the bar read ~0 → height `4 + 0 = 4px`, next to
  neighbours up to 44px. That stub is the break.

Compounding it: a live mic always has a room noise floor, so `micLevel` never
bottoms out; synthesized speech contains **true digital silence**, so
`speakLevel` genuinely reaches zero.

**This was not the bug.** The description above is accurate — speaking really
does skip half the audio, and `speech_orb.py` really does smooth it away
(`_tick()`: `follow = 0.5 if rising else 0.12`) — but it is not what the eye
was seeing, and the firmware change made no visible difference. It was
reverted. Kept here so the same theory isn't re-derived from scratch.

## The event loop must never block during a turn

Fixed 2026-09-07, after a cold start dropped a turn with

    Rina: Hey there, what's up? 😊
    ! turn failed: received 1011 (internal error) keepalive ping timeout

The reply was generated and then thrown away, because the *connection* died
while it was being produced.

**Mechanism.** `websockets.serve()` is called with no `ping_interval` /
`ping_timeout`, so both default to 20s. Every heavy stage of a turn —
`gate.check`, `stt.transcribe`, `stream_reply`, `voice.synth` — was called
synchronously inside the coroutine, so the loop could not answer the Stick's
keepalive ping for the whole turn. Past 20s websockets declares the peer dead,
and the `await ws.send("end")` in the `finally` then raises.

**What made that turn slow** was the first `rina` load after a reboot: 5.2 GB
off a cold page cache, far slower than a warm ~10s load. Measured, for scale:
speaker gate **0.10s**, whisper transcribe **1.54s** (whisper and VITS load at
startup, so neither is inside a turn). So the model load was essentially the
whole thing. `OLLAMA_KEEP_ALIVE=24h` keeps it resident afterwards.

**Fix**: `iter_in_thread()` and `call_in_thread()` in `bridge_server.py`. The
blocking work runs in a worker thread; the loop stays free to answer pings —
and to actually send `status:` updates *while* the model works, instead of
queueing them behind it. `iter_in_thread`'s queue is **unbounded on purpose**:
a bounded one deadlocks if the consumer stops early, because the producer
blocks forever in `put()` with nothing draining it.

This was latent for any slow turn and would have been constant for an agent
backend running tools. Raising the ping timeout would have hidden it; the loop
being blocked is the actual defect.

### The regression test took three attempts — the first two were worthless

Both **passed with the bug deliberately reintroduced**:

1. Asserting "some heartbeats happened" — satisfied by ticks that land
   *before* the blocking stage starts.
2. Counting heartbeats in a window "during" the block — **a stalled event loop
   cannot observe its own stall**, so the block completes before any coroutine
   is scheduled to look.

What works is measuring it afterwards: the heartbeat records the gap between
its own consecutive ticks, and an inline stage shows up as one long gap
(`max(gaps) < BLOCK / 2`). Confirmed in both directions — fails at a 1.00s
stall with the blocking call restored, passes with the fix.

**Always confirm a regression test fails without the fix.** Two of three
plausible-looking tests here did not.

## Clock face (`firmware/m5stick_bridge/src/clock_face.h`)

A Catppuccin Macchiato pixel-art clock, added 2026-09-08. **BtnA tap cycles to
it; BtnA hold still talks.**

- Screen is **240x135 landscape** (`setRotation(1)`), drawn into the existing
  shared `canvas` and pushed as one frame, so there is no partial-draw flicker
  to design around. `clockFaceTick()` re-composes **only when something visible
  changed** — the minute, the battery reading, the link state, or the 1.4s
  twinkle — so an idle clock costs about one push a minute instead of the
  face's eleven a second.
- **Big digits are hand-drawn** from a 4x7 bitmap scaled x6 (24x42 px).
  M5GFX's `Font7` was rejected deliberately: it is a seven-segment face, i.e.
  exactly the conventional digital-clock look this is meant not to have. The
  small text reuses M5GFX's built-in **`Font0`**, already a 5x7 bitmap font, so
  no font dependency was added.
- Time is `configTzTime()` with **`EST5EDT,M3.2.0/2,M11.1.0/2`** — the DST
  rules live in the TZ string, so nothing in the code knows today's date. NTP
  starts in `connectNetwork()`, not `setup()`, because it needs an association
  first.
- **Sync is detected by checking the year, not Wi-Fi** (`clockHasTime()` wants
  `tm_year > 2023-1900`). Once the RTC is set the ESP32 carries the clock on
  its own, so the hotspot can come and go without the display going blank.
  Before the first sync it shows dashes on the same grid, so the layout does
  not jump when the sync lands.

### There is NO battery-backed RTC on this board — measured 2026-09-08

`M5.Rtc.isEnabled()` reports **false** on the real Stick (`[rtc] hardware RTC
present: no`, printed at boot). M5Unified's `RTC_Class::begin()` has no
`board_M5StickS3` case, so it falls through to probing for a PCF8563 on the
internal I2C bus, and nothing answers. So the only timekeeping is the ESP32's
own clock. What that means, precisely:

| situation | does the time survive? |
| --- | :---: |
| Wi-Fi drops while powered | **yes** — the system clock free-runs |
| power off, flat battery, reflash, hard reset | **no** — starts unset |

After a cold boot the face shows four dashes until NTP lands; there is no chip
to restore from, and adding one would mean hardware, not code.

Drift is the ESP32's RTC slow clock, so the clock is only as good as its last
sync. That mostly does not matter because `configTzTime()` starts the ESP-IDF
SNTP client, which **re-polls on its own** — whenever the hotspot has upstream
internet again the clock corrects itself with no code involved.

### BtnA had to stop starting the recording on the press itself

A tap and a hold are identical at the instant of the press, so recording now
waits out `TALK_HOLD_MS` before starting; a release before that is a tap and
cycles the screen. This is not lost audio in practice — `M5.Mic.begin()` has
to spin the I2S driver up regardless, and people press before they speak.
Interrupting playback stays on the raw press, because that should be instant.

### Layout numbers, checked rather than eyeballed

Mantle plate spans x 46..194, y 24..86; the time block is 128px wide, centred
at x 56. Two accent dots were originally at x 190 and x 50, i.e. *on* the
plate — caught by asserting each decoration's bounding box against the plate's
rather than by looking at it, and moved outside.

Costs **+7 KB flash** (84.1% used at the time) and no measurable RAM (15.8%,
unchanged) — it reuses the canvas that already exists.

## Pomodoro screen (`firmware/m5stick_bridge/src/pomodoro_face.h`)

Added 2026-09-08, a third BtnA screen. **BtnA tap cycles Rina -> clock ->
pomodoro -> Rina**; **BtnB click starts/pauses the countdown while this
screen is showing**, instead of its usual "reset conversation" meaning, since
there is no conversation in view to reset here.

**Default 50 min focus / 10 min break**, changed from an initial 25/10 during
the same session, per request — `POMO_FOCUS_MS` / `POMO_BREAK_MS` in
`pomodoro_face.h`, both plain constants.

- **BtnB double-click resets to a fresh 50:00 FOCUS**, stopped rather than
  left running unnoticed. This needed `wasSingleClicked()`/
  `wasDoubleClicked()` instead of the fast `wasClicked()` used elsewhere:
  read from M5Unified's `Button_Class.cpp`, `wasClicked()` fires on **every**
  raw release, including each half of a double-click, so using it for
  start/pause would toggle running twice before the reset ever landed.
  `wasSingleClicked()`/`wasDoubleClicked()` instead wait out the ~500ms
  multi-click window and fire exactly once, decided. This only applies on the
  pomodoro screen — Rina/clock's plain reset-conversation keeps the instant
  `wasClicked()`, since that's the one action where the wait would be
  noticeable. (An earlier draft of this clock screen tried the opposite
  scoping — double-click for the alt-screen toggle, forcing every reset onto
  `wasSingleClicked()` — and was reverted for exactly this latency; recorded
  here so the same tradeoff isn't rediscovered from scratch.)
- **Reuses the clock's own digit font and grid** (`drawBigDigit()`, the
  `CLK_*` spacing constants) for MM:SS rather than a second font tuned to
  look similar — literally the same code path, so the two screens cannot
  visually drift apart the way two independent implementations eventually
  would.
- **The colon and the bottom progress bar carry the phase colour** — `COL_RED`
  (Catppuccin's own Red, already defined for the error state) while
  focusing, `COL_GREEN` on break — so the pomodoro identity survives even
  though the grid is borrowed wholesale from the clock, where those same
  blocks are always mauve.
- **The countdown advances independently of which screen is on top.**
  `pomodoroTick()` is called unconditionally every `loop()`, not just while
  the pomodoro screen is showing, so switching to Rina or the clock
  mid-session doesn't pause it by accident. `pomodoroFaceTick()` (the
  diff-and-redraw half) is the one gated on `currentScreen ==
  SCREEN_POMODORO`.
- Where the clock shows the date, this screen shows the phase name (`FOCUS`/
  `BREAK`) plus a **hand-drawn play/pause glyph** rather than a second word.
- Where the clock's bottom-left has a decorative wave row, this screen has a
  **10-segment progress bar** for the current phase, filled in the phase
  colour.

### `clockMode` became a 3-way `AltScreen` enum, and battery/twinkle moved out

`AltScreen` (`SCREEN_RINA` / `SCREEN_CLOCK` / `SCREEN_POMODORO`) replaces the
old boolean, declared in `clock_face.h` since it's the first alt-screen header
included and the natural home for state every alt screen shares.

Battery reading, Wi-Fi-link twinkle, and NTP-sync detection used to live
inside `clockFaceTick()`, so they only refreshed while the clock happened to
be the screen on top. Pomodoro needs the same battery reading, and would have
shown a stale one if the clock was never opened. Pulled out into
`screenAmbientTick()`, called unconditionally every `loop()`; `clockFaceTick()`
and `pomodoroFaceTick()` both just read the now-always-fresh globals for their
own diffs.

Likewise the clock's top decorations (crescent, sparkles, corner dots) are now
`drawAmbientDecorations()`, called by both `drawClockFace()` and
`drawPomodoroFace()` — one set of coordinates, not two copies that could drift.

**Each screen keeps its own separate redraw-diff cache** (`clockLast*` vs.
`pomoLast*`) rather than sharing one: sharing would make switching between two
screens skip a redraw whenever they happened to agree on a value (e.g. the
same battery percentage), which is exactly the class of bug a shared cache
would hide until someone was standing there watching for it.

Costs **+1.6 KB flash** on top of the clock and no measurable RAM.

## BtnB long-press deep sleep — removed 2026-09-15

BtnB's long-press used to trigger true ESP32 deep sleep (µA-range draw),
under the theory that "the Stick has no PMIC-assisted power button." That
theory turned out to be wrong: the physical power button on the M5StickS3
does a real PMIC-level power-off, confirmed against M5Stack's own docs
(`docs.m5stack.com/en/arduino/m5sticks3/button`) — single press powers on,
double-click powers off, holding it enters USB-C flashing boot mode. A true
PMIC power-off draws less than ESP32 deep sleep too, since deep sleep still
keeps the RTC/wake-monitoring domain powered.

With the real power button confirmed, the software-only deep-sleep
substitute was redundant and removed entirely: `configureWakeAndSleep()`,
`enterDeepSleep()`, the EXT0-wake re-check in `setup()`, the `UI_SLEEPING`
UI state, and the `<esp_sleep.h>`/`<driver/rtc_io.h>` includes are all gone.
BtnB keeps its click (reset conversation / pomodoro start-pause) and
double-click (pomodoro reset) behavior unchanged — those are unrelated
features that only shared the word "double-click" with the power button in
conversation, not in code. Flash usage actually dropped slightly (84.1% ->
82.3%) from the removal.


## Settings and firmware updates over BLE (2026-09-24)

**Volume and brightness are live settings now**, no rebuild. `stick_settings.h`:
the companion app's sliders send a `SETTINGS` frame (`0x0B`, `[1][volume][brightness]`);
the Stick applies it in `loop()` (never in the NimBLE callback, which would race
the display/speaker), saves it in NVS (namespace `stick`), and reports back the
applied values plus its firmware version (`FW_VERSION`, from `git describe` at
build time) after every change and whenever the link comes up. Defaults are the
old hardcoded 255 volume / 38 brightness; brightness never goes below 8 (a black
screen looks like a dead Stick). The app warns above volume 191, M5Stack's
battery brownout guidance.

**Firmware updates over BLE**, `ota_update.h` + `RelayService.startOta()`:

- The partition table is `default_8MB.csv`: two 3.2 MB app slots. It keeps
  `nvs`/`otadata`/`app0` at the same offsets as the old `huge_app.csv`, so bonds
  and settings survive. **Switching to it needs one last USB flash**; after that,
  updates go over Bluetooth. The image is ~2.27 MB (68% of a slot).
- Frames: `OTA_BEGIN 0x0C` `[u32 size][32-char MD5 hex]`, `OTA_DATA 0x0D`
  `[u32 offset][bytes]` (2 KB chunks, written without response), `OTA_END 0x0E`,
  and the Stick's `OTA_STATUS 0x0F` `[code][u32 value][text]` (READY, PROGRESS
  every 8 KB written, DONE, ERROR). The phone keeps at most 24 KB unacknowledged;
  the Stick stages into a 64 KB PSRAM stream buffer and writes flash from
  `loop()`, so the staging buffer can't overflow and flash writes never block
  the BLE task. Only accepted after AUTH (the shared secret is the gate; images
  are not signed).
- The new image goes into the idle slot and is checked (size, MD5, ESP image
  header by `Update`) before it becomes the boot slot; a failed or interrupted
  transfer leaves the running firmware untouched.
- **Rollback**: the framework's bootloader has `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`.
  `verifyRollbackLater()` returns true so Arduino doesn't mark the new image good
  at boot; it is marked good only when a phone passes AUTH on it. A crash before
  that, or no AUTH within 10 minutes, rolls back to the previous firmware. This
  covers the main way an update could brick the link: an image built with a
  different `secrets.h` than the app. (It also means an update installed with the
  app closed for 10 minutes rolls back; just update again.)
- While an update runs the relay closes the gateway WebSocket (no conversation)
  and the Stick shows a progress screen.

How to update: on the machine with `include/secrets.h`, `pio run` in
`firmware/m5stick_bridge`, copy `.pio/build/m5stick-s3/firmware.bin` to the phone,
then in the app: Start (connected), **Update Stick firmware...**, pick the file.
About a minute at the measured BLE rate (not yet timed on the device).

**Status: verified on the device, 2026-09-24.** Serial log + app screen:

1. USB flash of `a36b55d` with `default_8MB.csv`: booted, `[settings] volume=255
   brightness=38 firmware=a36b55d` (first boot logs `nvs_open failed: NOT_FOUND`
   once, harmless: the `stick` namespace didn't exist yet). The bond survived:
   reconnected with `bonded=1` on the first attempt. App 1.2 showed the version.
2. Sliders: `[settings] applied volume=197 brightness=128` live; after a reset the
   Stick booted with the same values and the app showed them.
3. OTA of a rebuilt image (`a36b55d-dirty`): 2,268,224 bytes received and
   verified in ~30 s (~75 KB/s), rebooted into `app1`, `pending verification`,
   AUTH passed, `rollback cancelled`. Settings carried over. The app asks for
   confirmation ("Update the Stick?") before starting.
4. Wrong-secret image: booted into `app0`, `AUTH frame: REJECTED`, then at
   exactly 600 s `no AUTH on the new firmware; rolling back`, rebooted into the
   previous image and the phone's AUTH was accepted immediately.

Then a clean `a36b55d` went back on over OTA, and volume/brightness were reset
to 255/39.
First test after the USB flash: the version shows in the app, both sliders take
effect and survive a reboot, then an OTA of a rebuilt image (version changes),
then an OTA of a deliberately wrong-secret image to see the rollback.
