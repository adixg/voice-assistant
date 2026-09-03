#!/usr/bin/env python3
"""Minimal stand-in for bridge_server.py that skips STT/Ollama/VITS entirely:
whatever the M5StickS3 records, it plays straight back. Speaks the exact same
WebSocket protocol as bridge_server.py, so this isolates "is it the Wi-Fi/
WebSocket/firmware audio path" from "is it the STT/LLM/TTS pipeline" — if
this doesn't round-trip cleanly, the problem is in the network or the
firmware's mic/speaker handling, not in whisper/ollama/VITS.

No conda env needed — stdlib + `websockets` only (same as bridge_server.py's
`chat` env, but nothing else from it is required).

    python tools/echo_server.py --ws-port 8765

For the pure-hardware version with no Wi-Fi/server at all, flash
firmware/m5stick_echo_test/ instead.
"""
import argparse
import asyncio

import websockets


async def handle_client(ws):
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
            print("  recording...", flush=True)
        elif msg == "stop":
            recording = False
            n = len(buf)
            print(f"  got {n} bytes ({n / 2 / 16000:.2f}s @ 16kHz) — echoing back", flush=True)
            await ws.send(f"reply:(echo, {n} bytes)")
            for off in range(0, len(buf), 4000):
                await ws.send(bytes(buf[off:off + 4000]))
            await ws.send("end")
        else:
            print(f"  ? unexpected control message: {msg!r}")
    print("  Stick disconnected", flush=True)


async def main_async(args):
    print(f"  listening on ws://{args.ws_host}:{args.ws_port} — waiting for the Stick...", flush=True)
    async with websockets.serve(lambda ws: handle_client(ws), args.ws_host, args.ws_port, max_size=None):
        await asyncio.Future()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws-host", default="0.0.0.0")
    ap.add_argument("--ws-port", type=int, default=8765)
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
