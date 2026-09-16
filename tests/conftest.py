"""Shared test setup.

Both chat_loop.py and bridge_server.py call voicepipe.cuda.ensure_cuda_libs()
at import time, which re-execs the process (os.execv) if it finds CUDA libs
that need adding to LD_LIBRARY_PATH. That's fine for those entrypoints but
would hijack the pytest process itself the moment a test imports
bridge_server. Setting its re-exec guard env var before any test module
imports bridge_server makes it a no-op here.
"""
import os

os.environ["_VOICEPIPE_CUDA_REEXEC"] = "1"


class FakeWebSocket:
    """Stand-in for a websockets connection, shared by every wire-protocol
    test (bridge_server.py, tools/echo_server.py, tools/termux_relay.py all
    speak variants of the same start/binary/stop/reset framing): async-
    iterable over a preset message sequence, and collects everything sent
    back or closed."""

    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self.sent = []
        self.closed = False
        self.remote_address = ("203.0.113.5", 12345)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def send(self, msg):
        self.sent.append(msg)

    async def send_text(self, text):
        """FastAPI/Starlette's WebSocket has send_text/send_bytes instead of
        websockets' single send() -- services/gateway/app.py uses these, so
        this fake collects both into the same `sent` list either way."""
        self.sent.append(text)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
