#!/usr/bin/env python3
"""Make the Stick say something, unprompted.

The bridge holds a persistent WebSocket to the Stick and its wire protocol
doesn't care who started a turn, so anything that can write to the bridge's
announce socket can speak through it — a cron job, an agent tool, or you:

    python tools/say.py "the build finished"
    echo "rain in twenty minutes" | python tools/say.py

Exits non-zero if the bridge isn't running or no Stick is connected, so a
script can tell whether it was actually heard.
"""
import argparse
import os
import socket
import sys
import tempfile

DEFAULT_SOCKET = os.path.join(tempfile.gettempdir(), "rina-announce.sock")


def say(text, path=DEFAULT_SOCKET, timeout=120):
    """Send one line; return the bridge's reply. Raises on no bridge."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(text.replace("\n", " ").encode("utf-8") + b"\n")
        return s.recv(256).decode("utf-8", "replace").strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="*", help="what to say; omit to read stdin")
    ap.add_argument("--socket", default=DEFAULT_SOCKET, help=f"(default: {DEFAULT_SOCKET})")
    args = ap.parse_args()

    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        ap.error("nothing to say")
    try:
        result = say(text, args.socket)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit(f"no bridge listening at {args.socket} — is bridge_server.py running?")
    print(result)
    sys.exit(0 if result == "ok" else 1)


if __name__ == "__main__":
    main()
