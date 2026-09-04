# aigf

A voice assistant: mic → faster-whisper (STT) → Ollama (LLM) → VITS-Umamusume (TTS) → speaker.
Runs either through this machine's local mic/speaker (`chat_loop.py`) or through
an M5StickS3 over Wi-Fi (`bridge_server.py` + `firmware/m5stick_bridge/`).

## Layout

```
voicepipe/            the STT/LLM/TTS pipeline, plain importable modules — no
                       CLI, no audio I/O assumptions. This is the thing to
                       import when debugging "is it the speech pipeline?"
  registry.py             STTBackend/LLMBackend/TTSBackend interfaces + a
                          name -> factory registry (see "Swapping backends"
                          below) — stt.py/llm.py/tts.py each register their
                          backend as a side effect of being imported
  stt.py                  faster-whisper load/transcribe, + FasterWhisperSTT
  llm.py                  ollama ask() + the default persona, + OllamaLLM
  tts.py                   VITS worker wrapper (Voice.synth() -> wav paths),
                          registered directly as the "vits" TTS backend
  audio.py                local mic/speaker helpers (pulse/ffplay), used by
                          chat_loop.py only — bridge_server.py's audio comes
                          over a WebSocket instead

chat_loop.py           local-mic terminal (+ optional "speech orb" GUI) entrypoint
bridge_server.py       M5StickS3 WebSocket bridge entrypoint
tts_cli.py              headless VITS worker, shelled out to from voicepipe.tts
                         (own conda env, see requirements-uma-tts.txt)
speech_orb.py           the animated PySide6 orb chat_loop.py shows

tools/
  echo_server.py        same WebSocket protocol as bridge_server.py, but skips
                         STT/Ollama/VITS entirely — mic audio goes straight
                         back to the speaker. Use this to tell a network/
                         firmware problem apart from a model problem.
  make_face_sprites.py  extracts the pixel-art face sheet into sprites.h
  make_test_clip.sh     regenerates m5stick_speak_test's embedded voice clip
  termux_relay.py        + termux_relay_setup.md — lets the Stick reach the
                         laptop over Tailscale when they're not on the same
                         Wi-Fi (runs on the phone, in Termux)

firmware/
  m5stick_bridge/        the real push-to-talk firmware (talks to bridge_server.py)
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

archive/                superseded experiments (OpenVoice, Kokoro) kept for reference
VITS-Umamusume-voice-synthesizer/   cloned HF Space (model code + weights)
```

## Running the tests

```bash
conda activate chat
pytest tests/
```

Covers the pure/mockable logic: `voicepipe.llm` (`strip_think`, `ask` against a
mocked Ollama client), `voicepipe.audio` (`_rms16`), `voicepipe.tts` (`chunks`),
and `bridge_server.py`'s wire protocol (`Session.handle_utterance`,
`handle_client`) with STT/LLM/TTS and the WebSocket mocked out — no GPU, model,
or network needed, runs in well under a second. Run it after any change to
`voicepipe/` or `bridge_server.py`.

What's deliberately **not** covered: `voicepipe.stt.load_stt`/`transcribe`
(needs a real faster-whisper model), `voicepipe.tts.Voice` (needs the real
`uma-tts` subprocess), and anything in `firmware/` (no practical way to unit
test ESP32/M5Unified C++ without a hardware simulator or a large native-mock
scaffold — not worth building for a project this size). The three-tier
hardware test path below is the practical equivalent for the firmware side.

## Swapping backends

`chat_loop.py` and `bridge_server.py` don't import a concrete STT/LLM/TTS
implementation — they ask `voicepipe.registry` for one by name:

```bash
python chat_loop.py --llm-backend ollama --stt-backend faster-whisper --tts-backend vits
```

Those are the only backends registered today (hence the only `--help`
choices), but adding one is just a class + a registration call, e.g. in a new
`voicepipe/hermes.py`:

```python
from voicepipe.registry import LLM

class HermesLLM:
    def ask(self, messages, think=None):
        ...  # e.g. an Ollama call with tools=[...] and a tool-execution loop
    def check(self):  # optional — see OllamaLLM.check() for the pattern
        ...

LLM.register("hermes")(HermesLLM)
```

Import that module once (from an entrypoint, or add it next to the other
`from voicepipe.llm import ...` lines) and `--llm-backend hermes` becomes a
valid choice — nothing else in `chat_loop.py`/`bridge_server.py` changes. The
interfaces (`STTBackend.transcribe`, `LLMBackend.ask`, `TTSBackend.synth`/
`close`) are structural (`typing.Protocol`), so a backend class doesn't need
to inherit from anything, just match the method(s).

## Debugging the speech pipeline

Each `voicepipe` module runs standalone:

```bash
conda activate chat
python -m voicepipe.stt some.wav                       # STT only
python -m voicepipe.llm "hi there" --model rina         # LLM only
python -m voicepipe.tts "hello there" -s 10 -o /tmp/hi.wav  # TTS only (writes wavs, doesn't play)
```

## Debugging the M5StickS3

From most to least isolated:

1. **`firmware/m5stick_echo_test/`** — flash this alone. Hold BtnA, talk,
   release, hear it played back. No Wi-Fi, no server, nothing but the mic,
   codec, and speaker. If this doesn't sound right, it's a hardware/firmware
   issue, not network or models.
2. **`tools/echo_server.py`** + `firmware/m5stick_bridge/` — the real
   firmware, but talking to the echo server instead of `bridge_server.py`.
   Isolates the Wi-Fi/WebSocket path from STT/Ollama/VITS.
3. **`bridge_server.py`** + `firmware/m5stick_bridge/` — the real thing.

See `firmware/m5stick_bridge/include/secrets.h.example` for the Wi-Fi/server
config the Stick needs (copy to `secrets.h`, gitignored).

## Using the Stick away from the laptop

By default the Stick and this laptop need to be on the same Wi-Fi (`WS_HOST`
in `secrets.h` is the laptop's IP on that network). When they're not — the
Stick and phone are elsewhere while the laptop stays put — see
`tools/termux_relay_setup.md`: a small relay running on the phone (in Termux)
forwards the Stick's WebSocket traffic to the laptop over Tailscale, so the
Stick still only ever talks to a plain `ws://` address on its own hotspot.

## Setup

`./setup_envs.sh` creates the two conda envs (`chat`, `uma-tts`) and clones the
VITS-Umamusume Space. See its header comment and `requirements-*.txt` for details.
