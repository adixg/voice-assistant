# Setup and using the Stick

This is a hardware-specific, self-managed deployment. Moved from the main README so it stays short; the diagrams are in [the README](../README.md#architecture).

## Setup

`./setup_envs.sh` creates the three conda envs (`chat`, `chatterbox-tts`,
`uma-tts`) and clones the VITS-Umamusume Space. See its header comment and
`requirements/*.txt` for details.

## Using the Stick

The Stick only talks BLE to the phone. Set the Android app's host to the
always-on k3s gateway (`arch-ssd.<tailnet>.ts.net`, port 30800); install/update
the app, open it, and tap **Start**. The service reconnects BLE and WebSocket
failures automatically. Host and port remain editable: point them at any
machine running `bridge_server.py` on port 8765 for a standalone fallback,
without reflashing the Stick.

## What the M5Stick does today

- **Hands-free listening** — hold BtnB, let go, and just talk: the Stick
  detects when you start and stop speaking (on-device, `vad.h`) and sends only
  the speech. Saying nothing cancels quietly; BtnA ends it early. Saying "Assistant"
  starts the same mode, through an on-device wake word.
- **Voice chatbot** — hold BtnA to talk, release to send; STT → LLM → TTS →
  speaker, same pipeline as the desktop entrypoint.
- **Speaker verification** ("voice recognition") — replies are gated to the
  enrolled owner's voice (WeSpeaker ECAPA-TDNN-512), so someone else in the
  room doesn't get an answer. Built, but currently switched off in the cluster
  deployment. See `docs/voice-pipeline.md`.
- **Three screens, cycled by tapping BtnA** (a hold still talks from any of
  them): Rina's animated pixel-art face, a Catppuccin-themed **clock**
  (no on-board RTC, so it's set from a `TIME_SYNC` BLE frame the phone sends
  on connect rather than NTP), and a **pomodoro timer** (50 min focus / 10 min
  break by default — BtnB click starts/pauses, BtnB double-click resets).
- **Proactive/unprompted speech** — reminders, encouragement, or a
  build-finished ping can all speak without a button press (see "Unprompted lines" in `development.md`).
- **BLE transport** — the Stick talks to the Android relay entirely over
  Bluetooth Low Energy now (no Wi-Fi at all in `firmware/m5stick_bridge/`):
  bonding plus an app-layer shared-secret handshake, relayed to
  the k3s gateway's WebSocket by the `android_companion/` phone app. Full
  design and the real-hardware verification log: `docs/ble-migration.md`.
- Powering the device off is the **physical power button** (double-click),
  not a firmware feature — see `docs/firmware-notes.md`.

Still in progress: an IMU wrist-raise gesture
wake, haptic feedback, voice isolation, and a BLE soak test (hours-long
connection, reconnect after reboot/deep-sleep; Start → Stop → Start and
Bluetooth-toggle reconnects are already verified on the device).

### On-device screens

The Stick cycles between Rina's animated face, a clock, and a pomodoro timer
with BtnA. The repository includes the source sprite and real M5StickS3
captures of the three screens:

<table>
  <tr>
    <th>Rina</th>
    <th>Clock</th>
    <th>Pomodoro</th>
  </tr>
  <tr>
    <td><img src="assets/sprites/idle.png" alt="Rina-chan pixel-art face" width="240"></td>
    <td><img src="assets/screens/clock-device.png" alt="M5StickS3 digital clock screen" width="240"></td>
    <td><img src="assets/screens/pomodoro-device.jpg" alt="M5StickS3 50-minute pomodoro focus timer" width="240"></td>
  </tr>
</table>

## Current Kubernetes path

The production route is:

```text
M5StickS3 → BLE → Android relay → gateway NodePort 30800
  → STT / agent / TTS services → scheduler-selected llama.cpp server
```

The k3s cluster has two GPU nodes. `arch-ssd` is the always-on GTX 1650 node;
the laptop node is an optional RTX 4060 worker. The GPU scheduler patches the
agent's `LLM_HOST` and `LLM_MODEL`: the preferred route is `qwen3-8b` on the
RTX 4060, with `qwen3.5-4b` on the GTX 1650 as the fallback. MCP remains
available on both routes because the MCP server runs in the CPU-side agent
container, independently of the model server. STT, TTS, gateway, agent,
and the core observability services are pinned to `arch-ssd`; the GPU exporter
still runs on both GPU nodes. The laptop can therefore disappear without
taking down the primary route or monitoring. See
[`deploy/kubernetes/README.md`](../deploy/kubernetes/README.md).

### One turn, in each GPU mode

The loop is the same in both modes; the only thing that changes is which
llama.cpp server the `agent` calls, and therefore where the LLM step runs.

**Laptop offline — GTX 1650 only (fallback).** Every hop after the phone stays
on `arch-ssd`. STT and TTS run on CPU, so the 4B model has the 1650 to
itself; when the laptop drops, that model is first cold-started from zero
(about 2.5 minutes to the first good reply, measured 2026-11-11) unless
`tools/standby.sh warm` pinned it (30 s).

**Laptop Ready — RTX 4060 online (preferred).** The same services stay on
`arch-ssd`; only step 5 moves. The `agent` calls the 8B model on the laptop,
crossing nodes over the flannel VXLAN overlay on `tailscale0`, and after 60 s
the controller scales `llama-cpp-gtx1650` to zero, leaving the 1650 idle.

Switching modes is automatic. `gpu-scheduler` re-evaluates every 10 seconds and
on every 4060 node event, from the whole picture rather than one transition: the
4060 counts as up only when its node is `Ready` **and** `llama-cpp-rtx4060` is
serving. Then:

- **4060 up**: it patches `agent` to `LLM_HOST=http://llama-cpp-rtx4060:8080/v1`,
  `LLM_MODEL=qwen3-8b`, and once that rollout has finished **and the 4060 has been
  up for 60 seconds** it scales `llama-cpp-gtx1650` to zero, freeing about 1 GiB
  of RAM and 3.4 GiB of VRAM on the home server. The 60 s debounce keeps a
  laptop that sleeps and wakes from making the 1650 reload its model every time.
- **4060 lost** (`NotReady`, deleted, or its server down): it scales the 1650
  standby back up first, and only points `agent` at
  `http://llama-cpp-gtx1650:8080/v1`, `qwen3.5-4b` once the standby reports
  ready, so `agent` never targets a server that isn't there. From zero the
  standby is ready in about 70 to 80 s and the first request to a fresh server
  takes about 55 to 75 s more after the agent moves (2026-11-11), on top of the
  time k3s takes to notice
  the node is gone; turns during that window fail and should be retried.
- **Pinned warm**: `tools/standby.sh warm` keeps the standby running for an
  instant failover at the cost of its RAM/VRAM; `auto` hands it back.

Each patch to `agent` rolls its pod, so a turn in flight during a switch can fail.
Nothing the Stick or phone talks to moves in either case.

Reply audio is spoken a sentence at a time: the gateway synthesizes the next
sentence while it sends the previous one's audio, and the Stick starts playing
once 1.5 s of audio has arrived instead of after the whole reply (verified on
the device 2026-09-24).
