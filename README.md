# aicompanion

A voice assistant: mic → faster-whisper (STT) → llama.cpp (LLM) → VITS-Umamusume (TTS) → speaker.
The primary device path is M5StickS3 → BLE → Android relay → the k3s gateway
and split STT/agent/TTS services. `bridge_server.py` remains the standalone
development fallback, and `chat_loop.py` runs the same backends with this
machine's local mic/speaker. TTS is
swappable (`--tts-backend vits` (default) or `chatterbox`, which can clone a
voice from a reference clip) — see "Swapping backends" below.

## What the M5Stick does today

- **Voice chatbot** — hold BtnA to talk, release to send; STT → LLM → TTS →
  speaker, same pipeline as the desktop entrypoint.
- **Speaker verification** ("voice recognition") — replies are gated to the
  enrolled owner's voice (WeSpeaker ECAPA-TDNN-512), so someone else in the
  room doesn't get an answer. See `docs/voice-pipeline.md`.
- **Three screens, cycled by tapping BtnA** (a hold still talks from any of
  them): Rina's animated pixel-art face, a Catppuccin-themed **clock**
  (no on-board RTC, so it's set from a `TIME_SYNC` BLE frame the phone sends
  on connect rather than NTP), and a **pomodoro timer** (50 min focus / 10 min
  break by default — BtnB click starts/pauses, BtnB double-click resets).
- **Proactive/unprompted speech** — reminders, encouragement, or a
  build-finished ping can all speak without a button press (see "Unprompted
  lines" below).
- **BLE transport** — the Stick talks to the Android relay entirely over
  Bluetooth Low Energy now (no Wi-Fi at all in `firmware/m5stick_bridge/`):
  bonding plus an app-layer shared-secret handshake, relayed to
  the k3s gateway's WebSocket by the `android_companion/` phone app. Full
  design and the real-hardware verification log: `docs/ble-migration.md`.
- Powering the device off is the **physical power button** (double-click),
  not a firmware feature — see `docs/firmware-notes.md`.

Still tracked in `TODO.md`: wake word activation, an IMU wrist-raise gesture
wake, haptic feedback, voice isolation, and a BLE soak test (hours-long
connection, reconnect after Bluetooth toggle/reboot/deep-sleep).

## Architecture

The k3s route is the normal device path. The standalone entrypoints remain
useful for local development and isolated debugging.

```mermaid
flowchart TB
    stick["<b>M5StickS3</b><br>push-to-talk, mic + speaker<br>240×135 pixel-art UI<br>BLE peripheral, no Wi-Fi"]
    relay["<b>android_companion/</b><br>RelayService.kt, foreground service<br>BLE central + WebSocket client<br>host/port/secret set in-app"]
    gateway["<b>gateway</b><br>k3s on always-on node<br>NodePort 30800"]
    stt["<b>stt service</b><br>faster-whisper"]
    agent["<b>agent service</b><br>LLM turn-taking"]
    tts["<b>tts service</b><br>VITS"]
    controller["<b>gpu-scheduler</b><br>selects available llama.cpp"]
    llama1650[("<b>llama.cpp</b><br>GTX 1650 / qwen3.5:4b")]
    llama4060[("<b>llama.cpp</b><br>RTX 4060 / qwen3:8b")]

    stick <-->|"BLE GATT<br>bonded + AUTH secret"| relay
    relay <-->|"WebSocket<br>Tailscale"| gateway
    gateway -->|HTTP| stt
    gateway -->|HTTP| agent
    gateway -->|HTTP| tts
    agent --> llama1650
    agent -. "when laptop is Ready" .-> llama4060
    controller -. "patches agent target" .-> agent

    classDef device fill:#eff1f5,stroke:#7287fd,stroke-width:2px,color:#4c4f69
    classDef service fill:#dce0e8,stroke:#1e66f5,stroke-width:2px,color:#4c4f69
    classDef worker fill:#eff1f5,stroke:#fe640b,stroke-width:2px,color:#4c4f69
    class stick,relay device
    class gateway,stt,agent,tts,controller service
    class llama1650,llama4060 worker
```

`tools/termux_relay.py` (Termux, Wi-Fi hotspot + Tailscale) did this same
relay job before the BLE migration and still works standalone, but
`android_companion/` is what's actually used today — see "Using the Stick"
below.

In the standalone/local path, the heavy TTS models each run in their **own
conda env** as a long-lived
subprocess, because their torch/CUDA pins conflict with each other and with
the `chat` env. `voicepipe/subproc.py` owns that plumbing, so a backend only
declares the command to run. faster-whisper and the HTTP-based LLM backends
need no such isolation — whisper runs in-process and the model server is
reached over HTTP.

One turn over the gateway WebSocket, including where the on-device UI changes
state. `services/gateway/app.py` and the standalone `bridge_server.py`
implement the same wire protocol; on the Stick's side,
`android_companion/RelayService.kt` translates
it 1:1 to/from the BLE byte-envelope frames described in
`docs/ble-migration.md` (`START`/`STOP`/`HEARD`/`STATUS`/`REPLY`/
`AUDIO_CHUNK`/`END`), so the diagram below is accurate for both the pre-BLE
and current BLE setups — only the transport between "S" and "B" changed:

```mermaid
sequenceDiagram
    participant S as M5StickS3
    participant B as k3s gateway
    participant W as STT service
    participant O as agent + llama.cpp
    participant T as TTS service

    Note over S: BtnA held → listening
    S->>B: "start"
    S->>B: mic PCM chunks
    Note over S: release → thinking
    S->>B: "stop"
    B->>W: utterance.wav
    W-->>B: transcript
    B->>S: "heard:<transcript>"
    B->>O: messages + system prompt
    O-->>B: reply text
    B->>S: "reply:<text>"
    Note over S: speaking
    B->>T: text (one line per chunk)
    T-->>B: wav paths
    B->>S: reply PCM chunks
    B->>S: "end"
    Note over S: idle
```

Every turn sends exactly one `end`, including failures — the Stick stays in
its speaking state until it arrives.

## Current production path

The normal device route is:

```text
M5StickS3 → BLE → Android relay → gateway NodePort 30800
  → STT / agent / TTS services → llama.cpp GPU backend
```

The k3s cluster has two GPU nodes. `arch-ssd` (GTX 1650) is the always-on
node and the laptop/WSL node (RTX 4060) is optional. The GPU scheduler points
the agent at the RTX 4060 while that node is Ready, then falls back to the
GTX 1650. STT and the default VITS TTS service remain pinned to the GTX 1650
so the primary route continues to work when the laptop is offline. See
[`deploy/kubernetes/README.md`](deploy/kubernetes/README.md) for setup and
rollout commands.

## Observability

The cluster includes Prometheus, Grafana, Tempo, OpenTelemetry, kube-state-
metrics, and NVIDIA DCGM Exporter. Apply the application and collector
manifests from the repository root:

```bash
kubectl apply -f observability/prometheus/prometheus.yaml
kubectl apply -f observability/kubernetes-metrics.yaml
kubectl apply -f observability/tracing.yaml
kubectl apply -f observability/grafana/grafana.yaml
kubectl apply -f observability/grafana/dashboard.yaml
```

Prometheus scrapes the gateway, STT, agent, TTS, kube-state-metrics, and
DCGM Exporter every 15 seconds. Its data is stored on a 5 GiB PVC. Useful
metrics include `aicompanion_http_requests_total`, gateway turn and stage
latency histograms, and `DCGM_FI_DEV_GPU_UTIL` / `DCGM_FI_DEV_FB_USED` for
GPU and VRAM usage. The provisioned Grafana dashboard has a stable UID and
shows target health, request/error rate, HTTP and gateway latency, pod
readiness/restarts, GPU/VRAM usage, ElevenLabs usage, and traces:

```text
/d/aicompanion/ai-companion-service-performance
```

Open the UIs locally with:

```bash
kubectl -n aicompanion port-forward svc/prometheus 9090:9090
kubectl -n aicompanion port-forward svc/grafana 3000:3000
```

FastAPI services emit OpenTelemetry spans over OTLP gRPC to `tempo:4317`.
Tempo stores those traces on its own 5 GiB PVC, and Grafana reads them through
its provisioned Tempo datasource. This makes a completed turn traceable across
the gateway, STT, agent, and TTS stages. Grafana's default login is `admin` /
`admin`; change it before exposing Grafana beyond a local port-forward.

## MCP and Hermes control

`tools/companion_control_mcp.py` is a dependency-free, read-only MCP server.
Its tools expose service health, GPU/VRAM status, and agent health from the
existing Prometheus and agent HTTP APIs. The Qwen3 agent can also run the MCP
tool loop itself when `MCP_SERVER_COMMAND` is configured. See
[`docs/companion-control-mcp.md`](docs/companion-control-mcp.md) for the full
Hermes and in-cluster configurations. A local Hermes setup uses:

```yaml
mcp_servers:
  companion_control:
    command: /usr/bin/python3
    args: [/home/aditya/aigf/tools/companion_control_mcp.py]
    enabled: true
    trust: untrusted
    tools:
      include: [get_service_health, get_gpu_status, get_agent_status]
```

When Hermes runs on the host, port-forward Prometheus (and the agent if
needed) and set `COMPANION_CONTROL_PROMETHEUS_URL` and
`COMPANION_CONTROL_AGENT_URL` accordingly. The server has no shell,
filesystem, Kubernetes, or device-write capability.

## TTS backends and usage

The deployed default is `vits` (Umamusume). `chatterbox` remains available
for local voice cloning, and `elevenlabs` is an optional hosted backend. The
ElevenLabs backend reads `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`,
`ELEVENLABS_MODEL_ID`, and optionally `ELEVENLABS_CREDITS_REMAINING` from the
environment or a Kubernetes Secret; never commit those values. It exports
character-count and estimated-credit metrics to Prometheus. Select a backend
with the existing `--tts-backend` option or the corresponding service
configuration.

## Current component status

| Component | Current implementation |
| --- | --- |
| Device transport | M5StickS3 → BLE → Android relay |
| Gateway | k3s WebSocket gateway, NodePort 30800 |
| LLM serving | llama.cpp on GTX 1650 / optional RTX 4060 |
| Metrics | Prometheus, kube-state-metrics, DCGM Exporter |
| Dashboards | Grafana service-performance dashboard |
| Tracing | OpenTelemetry → Tempo → Grafana |
| Default TTS | Umamusume VITS |
| Optional TTS | Chatterbox and ElevenLabs |
| Control interface | Read-only companion-control MCP tools |

## Layout

```
voicepipe/            the STT/LLM/TTS pipeline, plain importable modules — no
                       CLI, no audio I/O assumptions. This is the thing to
                       import when debugging "is it the speech pipeline?"
  registry.py             STTBackend/LLMBackend/TTSBackend interfaces + the
                          name -> backend registry, including the per-backend
                          CLI plumbing (see "Swapping backends" below)
  backends/               the concrete engines, one file each, discovered
                          automatically — adding a file here is all it takes
    whisper.py              faster-whisper           ("faster-whisper", STT)
    ollama.py               local Ollama compatibility backend ("ollama", LLM)
    hermes_agent.py         Hermes Agent, or any OpenAI-compatible
                            endpoint (OpenRouter, vLLM, llama.cpp,
                            LM Studio, LiteLLM)      ("hermes-agent", LLM)
    chatterbox.py           Chatterbox Turbo         ("chatterbox", TTS)
    vits.py                 VITS-Umamusume           ("vits", TTS, default)
  cli.py                  the flags every entrypoint shares, assembled from
                          the registries — this is why no entrypoint mentions
                          a concrete backend
  __main__.py             `python -m voicepipe speak/transcribe/ask`, for
                          running one stage on its own
  subproc.py              shared worker plumbing for engines that live in
                          their own conda env (spawn, handshake, teardown)
  text.py                 sentence-aware chunking, shared by every TTS backend
  personas.py             the built-in system prompts
  encouragement.py        the unprompted lines behind `--encourage`
  audio.py                local mic/speaker helpers (pulse/ffplay), used by
                          chat_loop.py only — bridge_server.py's audio comes
                          over a WebSocket instead
  cuda.py                 LD_LIBRARY_PATH setup for GPU whisper

chat_loop.py           local-mic terminal (+ optional "speech orb" GUI) entrypoint
bridge_server.py       M5StickS3 WebSocket bridge entrypoint
chatterbox_cli.py       headless Chatterbox Turbo worker, shelled out to from
                         backends/chatterbox.py (own conda env, see
                         requirements/chatterbox.txt) — --tts-backend chatterbox
tts_cli.py              headless VITS worker, shelled out to from
                         backends/vits.py (own conda env, see
                         requirements/uma-tts.txt) — default TTS backend
speech_orb.py           the desktop face chat_loop.py shows — a port of the
                         Stick's own UI (same sprites, palette, particle
                         field and waveform bars), not a lookalike

assets/sprites/         the extracted pixel art, written by
                         tools/make_face_sprites.py alongside sprites.h —
                         the same crops, as PNGs, for speech_orb.py

memory/about-me.md      hand-written facts about you, appended to the system
                         prompt every conversation so she doesn't have to be
                         told them again (--profile / --no-profile)

android_companion/      the Android app that bridges the Stick's BLE
                         connection to bridge_server.py's WebSocket —
                         RelayService.kt (foreground service, BLE central +
                         OkHttp WebSocket client), BleEnvelopeCodec.kt (the
                         Kotlin side of ble_envelope.h's byte-envelope
                         codec), Prefs.kt (host/port/shared-secret settings,
                         persisted), MainActivity.kt (settings UI). See
                         docs/ble-migration.md.

tools/
  echo_server.py        same WebSocket protocol as bridge_server.py, but skips
                         STT/LLM/VITS entirely — mic audio goes straight
                         back to the speaker. Use this to tell a network/
                         firmware problem apart from a model problem.
  make_face_sprites.py  extracts the pixel-art face sheet into both
                         firmware/.../sprites.h (RGB565) and assets/sprites/
                         (PNGs) — one run, one set of crops, so the Stick and
                         speech_orb.py can't drift apart
  make_test_clip.sh     regenerates m5stick_speak_test's embedded voice clip
  termux_relay.py        + termux_relay_setup.md — the pre-BLE way to reach
                         the laptop over Tailscale (phone Wi-Fi hotspot +
                         Termux); superseded by android_companion/ but kept,
                         still works standalone

firmware/
  m5stick_bridge/        the real push-to-talk firmware — talks to
                         bridge_server.py over BLE via android_companion/
                         (ble_envelope.h / ble_transport.h; no Wi-Fi)
  m5stick_echo_test/     mic -> speaker loopback, on-device only, no Wi-Fi at
                         all — the fastest way to sanity-check the hardware
                         (mic, codec, speaker, volume) in isolation
  m5stick_speak_test/    plays one fixed pre-baked sentence (real Rina voice)
                         on button press — isolates the speaker/volume path
  m5stick_http_test/     standalone Wi-Fi/HTTP connectivity smoke test, no
                         relation to the WebSocket protocol — GETs a plain
                         HTTP server and reports Wi-Fi/TCP/HTTP status

tests/                  pytest suite for voicepipe/ and bridge_server.py's
                         wire protocol — see "Running the tests" below

docs/                   detailed investigation logs behind CLAUDE.md's
                         current-state summaries (hardware budget, the local
                         agent investigation, voice pipeline, firmware bugs,
                         the BLE migration, the k3s deployment architecture)
                         — CLAUDE.md itself is a lean index
TODO.md                 the active punch list

models/                 local Ollama Modelfiles (development compatibility path)
requirements/           the three conda envs' pinned dependencies

VITS-Umamusume-voice-synthesizer/   cloned HF Space (model code + weights)

services/               bridge_server.py split into HTTP services along
                         voicepipe/registry.py's existing STT/LLM/TTS
                         boundaries; this is the primary device path
                         (see docs/deployment-architecture.md)
  gateway/                 the Stick's protocol-compatible WebSocket peer
  stt/, agent/, tts/       thin FastAPI wrappers, one per registry entry
  metrics.py               shared Prometheus HTTP and gateway instrumentation
  telemetry.py             optional OpenTelemetry/Tempo setup
  tts/elevenlabs.py        optional hosted ElevenLabs TTS backend and usage metrics

tools/companion_control_mcp.py  read-only Prometheus/agent MCP control server
voicepipe/mcp_stdio.py          stdio MCP client/tool-loop support for the agent

deploy/kubernetes/      k3s manifests for the home-server (GTX 1650) +
                         laptop (RTX 4060) cluster
controller/gpu_scheduler/  custom controller that retargets the agent
                         service to whichever GPU node is up
observability/           Prometheus, Grafana, Tempo, OpenTelemetry, kube-state-
                         metrics, and DCGM exporter manifests
benchmarks/              benchmark scripts and comparison notes
deploy/helm/, deploy/argocd/  future GitOps packaging; not the active deploy path
```

## Running the tests

```bash
conda activate chat
pytest tests/
```

Covers the pure and mockable logic: the backend registry and its CLI plumbing,
`voicepipe.text` (chunking), `voicepipe.personas`, `voicepipe.backends.ollama`
(`strip_think`, `ask`/`check` against a mocked client), `voicepipe.audio`
(`_rms16`), `voicepipe.subproc` (the worker lifecycle, against a fake worker
script), `bridge_server.py`'s wire protocol with STT/LLM/TTS and the
WebSocket mocked out, the `services/*/app.py` HTTP wrappers (FastAPI's test
client against a fake backend, same seam as above), and
`controller/gpu_scheduler/controller.py`'s routing logic (a recording fake
in place of the Kubernetes client) — no GPU, model, cluster, or network
needed, runs in a few seconds. Run it after any change to `voicepipe/`,
`bridge_server.py`, `services/`, or `controller/`.

What's deliberately **not** covered: loading a real faster-whisper, VITS or
Chatterbox model (each needs its own conda env and a GPU), and anything in
`firmware/` beyond compiling (no practical way to unit test ESP32/M5Unified
C++ behavior without a hardware simulator or a large native-mock scaffold —
not worth building for a project this size; CI does still catch a build
break via `pio run`). The three-tier hardware test path below is the
practical equivalent for the firmware side.

## CI

`.github/workflows/ci.yml` runs on every push/PR: the pytest suite above,
building all five Docker images in `services/`/`controller/gpu_scheduler/`
(catches a broken Dockerfile — this dev environment has no working Docker
daemon to test them locally), applying every `deploy/kubernetes/` and
`controller/gpu_scheduler/deploy.yaml` manifest against a throwaway `kind`
cluster (real server-side schema validation, not just YAML syntax — the
pods won't actually schedule since a GPU-less kind node can't satisfy
`nvidia.com/gpu` requests, which is fine, `apply` doesn't wait for that),
and a `pio run` compile check of `firmware/m5stick_bridge/`.

## Swapping backends

`chat_loop.py` and `bridge_server.py` never name a concrete STT/LLM/TTS
implementation — they ask `voicepipe.registry` for one by name:

```bash
python chat_loop.py --llm-backend ollama --stt-backend faster-whisper --tts-backend vits
```

**Adding a backend is one file.** Drop it in `voicepipe/backends/` and it is
discovered, registered, listed in `--help`, and constructible with no edit to
any entrypoint:

```python
# voicepipe/backends/hermes.py
from voicepipe.registry import LLM

@LLM.register("hermes")
class HermesLLM:
    @staticmethod
    def add_arguments(group):        # optional: your own CLI flags
        group.add_argument("--hermes-tools", default="all")

    @classmethod
    def from_args(cls, args):        # optional: build from those flags
        return cls(tools=args.hermes_tools)

    def __init__(self, tools="all"):
        self.tools = tools

    def ask(self, messages, think=None):
        ...  # e.g. an Ollama call with tools=[...] and a tool-execution loop
```

`--llm-backend hermes` is now a valid choice everywhere, `--hermes-tools`
shows up under its own heading in `--help`, and `python -m voicepipe ask
--llm-backend hermes` works too. The interfaces (`STTBackend.transcribe`,
`LLMBackend.ask`, `TTSBackend.synth`/`close`) are structural
(`typing.Protocol`), so a backend inherits from nothing — it just needs the
methods. Backends take ordinary keyword arguments rather than an argparse
namespace, so they stay usable from plain Python; `from_args` is only the
adapter between the two.

A TTS engine that needs its own conda env (conflicting torch/CUDA pins, as
both current ones have) gets the subprocess plumbing for free by subclassing
`voicepipe.subproc.WorkerVoice` and implementing one method:

```python
@TTS.register("piper")
class PiperVoice(WorkerVoice):
    name = "piper"

    def command(self):
        return [PIPER_PYTHON, PIPER_CLI, "--serve", "--voice", self.voice]
```

## Unprompted lines

Nothing in the wire protocol cares who started a turn, so the server can speak
without the button being pressed — no firmware change needed.

```bash
python tools/say.py "the build finished"      # one-off, via the announce socket

python bridge_server.py --encourage           # a line of encouragement every 15-20 min
python bridge_server.py --encourage --encourage-interval 30 45
```

TTS audio is run through ffmpeg's `speechnorm` before it reaches the Stick —
the speaker is already at full volume, but the synthesizer leaves several dB
of headroom unused (a measured reply went from -4.5 dB peak to -0.4 dB).
`--no-normalize` sends it raw.

The encouragement lines live in `voicepipe/encouragement.py` — edit that list
to change what she says. They are static rather than model-generated on
purpose: instant, free, offline, and they can't wander off-persona at 3am.
The interval is drawn fresh from the range each time so it doesn't read as a
cron job, and an unprompted line waits for any turn in flight rather than
interleaving its audio with the reply's.

## Debugging the speech pipeline

Each stage runs on its own, with every backend's flags available:

```bash
conda activate chat
python -m voicepipe transcribe some.wav                  # STT only
python -m voicepipe ask "hi there" --model rina           # LLM only
python -m voicepipe speak "hello there"                   # TTS only (writes wavs, doesn't play)
python -m voicepipe speak "hello" --tts-backend vits --vits-speaker 10
```

## The desktop face

`chat_loop.py` opens `speech_orb.py`'s window automatically when a display and
PySide6 are available (`--no-orb` keeps everything in the terminal). It isn't a
separate design: it draws into the Stick's own 240×135 canvas using the same
sprites, the same Catppuccin Macchiato palette, the same procedural particle
field and waveform bars, then scales that up nearest-neighbour — so the two
screens show the same thing at different sizes.

```bash
python speech_orb.py              # demo: state buttons + live mic
python speech_orb.py --frameless  # translucent, draggable, always-on-top
```

If the window shows "no sprites", the art hasn't been extracted yet:

```bash
python tools/make_face_sprites.py
```

Because both outputs come from that one command, the Stick and the window
can't drift apart — changing a crop changes both.

## Debugging the M5StickS3

From most to least isolated:

1. **`firmware/m5stick_echo_test/`** — flash this alone. Hold BtnA, talk,
   release, hear it played back. No BLE, no phone, no server, nothing but
   the mic, codec, and speaker. If this doesn't sound right, it's a
   hardware/firmware issue, not network or models.
2. **`tools/echo_server.py`** + `firmware/m5stick_bridge/` + the
   `android_companion/` app — the real firmware and the real BLE/WebSocket
   path, but the phone app talks to the echo server instead of
   `bridge_server.py`. Isolates the BLE + WebSocket relay path from
   STT/LLM/VITS.
3. **k3s `gateway`** + `firmware/m5stick_bridge/` + `android_companion/`
   — the real deployment. Use `bridge_server.py` on port 8765 as the
   standalone equivalent when isolating cluster issues.

See `firmware/m5stick_bridge/include/secrets.h.example` for the
`BLE_SHARED_SECRET` the Stick needs (copy to `secrets.h`, gitignored) — it
must match the secret entered in the Android app's settings (`Prefs.kt`
defaults both sides to the same placeholder, so a fresh flash and a fresh
install interoperate out of the box).

## Using the Stick

The Stick only talks BLE to the phone. The Android app defaults to the
always-on k3s gateway at `arch-ssd.<tailnet>.ts.net:30800`; install/update
the app, open it, and tap **Start**. The service reconnects BLE and WebSocket
failures automatically. Host and port remain editable: point them at any
machine running `bridge_server.py` on port 8765 for a standalone fallback,
without reflashing the Stick. `tools/termux_relay.py` is the older Wi-Fi-era
alternative and is no longer the day-to-day route.

## Setup

`./setup_envs.sh` creates the three conda envs (`chat`, `chatterbox-tts`,
`uma-tts`) and clones the VITS-Umamusume Space. See its header comment and
`requirements/*.txt` for details.
