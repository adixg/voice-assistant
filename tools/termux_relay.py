#!/usr/bin/env python3
"""WebSocket relay for when the M5StickS3 and laptop are NOT on the same
Wi-Fi. Runs on the phone (in Termux), and chains:

    Stick --(phone hotspot, plain WebSocket)--> this relay
          --(Tailscale)--> ws://<laptop tailnet IP>:8765/ (bridge_server.py)

The Stick still just connects to a plain ws:// address on its own hotspot
(same as talking to bridge_server.py directly) — it never needs to know
about Tailscale. Only WS_HOST in firmware/m5stick_bridge/include/secrets.h
changes: point it at the phone's own gateway IP (WiFi.gatewayIP() on the
Stick) instead of the laptop's, then reflash. See
tools/termux_relay_setup.md for the full procedure.

    python termux_relay.py --laptop-host 100.70.0.38

No conda env needed — stdlib + `websockets` only (pip install websockets
inside Termux; see the setup doc).
"""
import argparse
import asyncio

import websockets


async def pump(src, dst):
    """Forward every message from src to dst until src closes, preserving
    text/binary framing (websockets' send()/recv() already round-trip that)."""
    try:
        async for msg in src:
            await dst.send(msg)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        try:
            await dst.close()
        except websockets.exceptions.ConnectionClosed:
            pass


async def handle_stick(stick_ws, args):
    laptop_url = f"ws://{args.laptop_host}:{args.laptop_port}/"
    print(f"Stick connected from {stick_ws.remote_address}; dialing {laptop_url}", flush=True)
    try:
        async with websockets.connect(laptop_url, max_size=None) as laptop_ws:
            print("  connected to laptop, relaying...", flush=True)
            await asyncio.gather(
                pump(stick_ws, laptop_ws),
                pump(laptop_ws, stick_ws),
            )
    except OSError as e:
        print(f"  ! couldn't reach the laptop at {laptop_url}: {e}", flush=True)
        await stick_ws.close()
    print("Stick disconnected", flush=True)


async def main_async(args):
    print(f"listening on ws://0.0.0.0:{args.listen_port}/  ->  "
          f"ws://{args.laptop_host}:{args.laptop_port}/", flush=True)
    async with websockets.serve(lambda ws: handle_stick(ws, args), "0.0.0.0",
                                 args.listen_port, max_size=None):
        await asyncio.Future()  # run forever


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--laptop-host", required=True, help="laptop's Tailscale IP, e.g. 100.70.0.38")
    ap.add_argument("--laptop-port", type=int, default=8765)
    ap.add_argument("--listen-port", type=int, default=8765, help="port the Stick connects to, on the phone")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
