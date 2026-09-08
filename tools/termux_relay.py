#!/usr/bin/env python3
"""WebSocket relay letting the M5StickS3 reach bridge_server.py on whichever
laptop is currently running it, over Tailscale. Runs on the phone (in
Termux), and chains:

    Stick --(phone hotspot, plain WebSocket)--> this relay
          --(Tailscale)--> ws://<laptop tailnet IP>:8765/ (bridge_server.py)

The Stick always joins the phone's own hotspot and finds this relay by
itself (it's always the Stick's Wi-Fi gateway — see WiFi.gatewayIP() in
connectNetwork(), firmware/m5stick_bridge/src/main.cpp) — nothing on the
firmware side ever needs to change. Switching which laptop is live is just
restarting this script with a different --laptop-host. See
tools/termux_relay_setup.md for the full procedure.

    python termux_relay.py --laptop-host main

No conda env needed — stdlib + `websockets` only (pip install websockets
inside Termux; see the setup doc).
"""
import argparse
import asyncio

import websockets

# Tailscale IPs of the machines that can run bridge_server.py — --laptop-host
# takes either of these names or a raw IP (e.g. for a laptop not listed here).
KNOWN_HOSTS = {
    "main": "100.70.0.38",     # this laptop
    "arch": "100.108.216.68",  # the other laptop
}


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
    ap.add_argument("--laptop-host", required=True,
                     help=f"one of {sorted(KNOWN_HOSTS)}, or a raw Tailscale IP")
    ap.add_argument("--laptop-port", type=int, default=8765)
    ap.add_argument("--listen-port", type=int, default=8765, help="port the Stick connects to, on the phone")
    args = ap.parse_args()
    args.laptop_host = KNOWN_HOSTS.get(args.laptop_host, args.laptop_host)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
