# aigf

A voice assistant: mic → faster-whisper (STT) → Ollama (LLM) → VITS-Umamusume (TTS) → speaker.
Runs either through this machine's local mic/speaker (`chat_loop.py`) or through
an M5StickS3 over Wi-Fi (`bridge_server.py` + `firmware/m5stick_bridge/`).

## Layout

```
voicepipe/            the STT/LLM/TTS pipeline, plain importable modules — no
                       CLI, no audio I/O assumptions. This is the thing to
                       import when debugging "is it the speech pipeline?"
  stt.py                 faster-whisper load/transcribe
  llm.py                 ollama ask() + the default persona
  tts.py                 VITS worker wrapper (Voice.synth() -> wav paths)
  audio.py               local mic/speaker helpers (pulse/ffplay), used by
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

firmware/
  m5stick_bridge/        the real push-to-talk firmware (talks to bridge_server.py)
  m5stick_echo_test/     mic -> speaker loopback, on-device only, no Wi-Fi at
                         all — the fastest way to sanity-check the hardware
                         (mic, codec, speaker, volume) in isolation

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

## Setup

`./setup_envs.sh` creates the two conda envs (`chat`, `uma-tts`) and clones the
VITS-Umamusume Space. See its header comment and `requirements-*.txt` for details.
