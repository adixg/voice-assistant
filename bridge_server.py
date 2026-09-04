#!/usr/bin/env python3
"""Voice bridge for the M5StickS3: same STT -> Ollama -> VITS pipeline as
chat_loop.py (via voicepipe/), but mic/speaker come from the Stick over a
WebSocket instead of this machine's local mic/ffplay. See
firmware/m5stick_bridge/ for the Stick side, and tools/echo_server.py for a
version of this that skips STT/LLM/TTS entirely (mic straight back out) —
useful for isolating a hardware/network problem from a model problem.

Wire protocol (one persistent WS connection, PCM16 mono @ 16kHz both ways):
  Stick -> server: text "start", then binary mic PCM chunks, then text "stop";
                   or text "reset" any time, to clear conversation history
  server -> Stick: text "heard:<transcript>" as soon as STT finishes, then
                   text "reply:<text>", then binary reply PCM chunks, then
                   text "end"

Run in the `chat` conda env (same as chat_loop.py):

    conda activate chat
    python bridge_server.py --host http://media:11434 --model rina

Needs on PATH: ffmpeg (for resampling VITS output to 16kHz for the Stick).
Needs the `uma-tts` env for tts_cli.py (shelled out, same as chat_loop.py).
"""
import argparse
import asyncio
import os
import sys
import wave

import websockets

from voicepipe.llm import DEFAULT_SYSTEM, ask
from voicepipe.stt import ensure_cuda_libs, load_stt, transcribe
from voicepipe.tts import TMP, Voice

ensure_cuda_libs()

SAMPLE_RATE = 16000
MIN_UTTERANCE_BYTES = SAMPLE_RATE * 2 // 4  # ignore stray <0.25s blips
SEND_CHUNK = 4000


async def resample_to_pcm16(wav_path):
    """ffmpeg any wav to raw 16kHz mono s16le bytes, for the Stick's I2S speaker."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", wav_path, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
    )
    data, _ = await proc.communicate()
    return data


class Session:
    """One M5StickS3's conversation state (this bridge assumes a single Stick)."""

    def __init__(self, client, stt, stt_lang, voice, args):
        self.client = client
        self.stt = stt
        self.stt_lang = stt_lang
        self.voice = voice
        self.args = args
        self.messages = []
        if args.system:
            self.messages.append({"role": "system", "content": args.system})

    async def handle_utterance(self, ws, pcm):
        if len(pcm) < MIN_UTTERANCE_BYTES:
            return
        wav_in = os.path.join(TMP, "bridge_in.wav")
        with wave.open(wav_in, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)

        text = transcribe(self.stt, wav_in, self.stt_lang)
        print(f"  you said: {text or '(nothing heard)'}")
        if not text:
            await ws.send("reply:(didn't catch that)")
            await ws.send("end")
            return

        # Not truly live/word-by-word (faster-whisper transcribes the
        # completed utterance, not a stream) — sent as soon as it's ready,
        # which lands right as the Stick's UI moves from listening to thinking.
        await ws.send(f"heard:{text}")

        self.messages.append({"role": "user", "content": text})
        try:
            reply = ask(self.client, self.args.model, self.messages,
                        None if self.args.think else False)
        except Exception as e:  # noqa: BLE001
            self.messages.pop()
            print(f"  ! ollama error: {e}")
            await ws.send(f"reply:(ollama error: {e})")
            await ws.send("end")
            return
        self.messages.append({"role": "assistant", "content": reply})
        print(f"  Rina: {reply}")

        await ws.send(f"reply:{reply}")
        if self.voice:
            for wav in self.voice.synth(reply):
                pcm_out = await resample_to_pcm16(wav)
                for off in range(0, len(pcm_out), SEND_CHUNK):
                    await ws.send(pcm_out[off:off + SEND_CHUNK])
        await ws.send("end")


async def handle_client(ws, session):
    print(f"  Stick connected: {ws.remote_address}", flush=True)
    buf = bytearray()
    recording = False
    async for msg in ws:
        if isinstance(msg, (bytes, bytearray)):
            if recording:
                buf.extend(msg)
            continue
        if msg == "start":
            recording = True
            buf = bytearray()
        elif msg == "stop":
            recording = False
            await session.handle_utterance(ws, bytes(buf))
        elif msg == "reset":
            session.messages = [m for m in session.messages if m["role"] == "system"]
        else:
            print(f"  ? unexpected control message: {msg!r}")
    print("  Stick disconnected", flush=True)


async def main_async(args):
    import socket
    from urllib.parse import urlparse
    import ollama

    u = urlparse(args.host)
    try:
        with socket.create_connection((u.hostname, u.port or 11434), timeout=4):
            pass
    except OSError as e:
        print(f"  ! can't reach {args.host}: {e}")
        sys.exit(1)

    client = ollama.Client(host=args.host, timeout=120)
    names = [m.model for m in client.list().models]
    print(f"  ollama @ {args.host} — {len(names)} models")
    if args.model not in names and f"{args.model}:latest" not in names:
        print(f"  ! '{args.model}' not found. available: {', '.join(names) or '(none)'}")

    stt = load_stt(args.whisper_model, args.whisper_device)
    stt_lang = None if args.stt_lang == "auto" else args.stt_lang

    voice = None
    if not args.no_voice:
        voice = Voice(args)
        voice.start()

    session = Session(client, stt, stt_lang, voice, args)

    print(f"  listening on ws://{args.ws_host}:{args.ws_port} — waiting for the Stick...", flush=True)
    async with websockets.serve(lambda ws: handle_client(ws, session),
                                 args.ws_host, args.ws_port, max_size=None):
        await asyncio.Future()  # run forever


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                     help="Ollama base URL (env OLLAMA_HOST), e.g. http://media:11434")
    ap.add_argument("--model", default="rina", help="Ollama model name (default: rina)")
    ap.add_argument("--system", default=DEFAULT_SYSTEM,
                     help="system prompt (defaults to the Rina persona); pass --system '' to send none")
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--whisper-model", default="small")
    ap.add_argument("--whisper-device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--stt-lang", default="en")
    ap.add_argument("--tts-model", default="trilingual", choices=["trilingual", "japanese"])
    ap.add_argument("--tts-lang", default="en", choices=["ja", "zh", "en", "mix", "none"])
    ap.add_argument("--tts-device", default="cpu")
    ap.add_argument("--speaker", type=int, default=10)
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--ws-host", default="0.0.0.0", help="bind address for the Stick's WebSocket")
    ap.add_argument("--ws-port", type=int, default=8765)
    args = ap.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
