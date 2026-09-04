"""tools/termux_relay.py's pump() — the bidirectional forwarding loop that
does the actual relaying (handle_stick() itself is mostly just wiring pump()
up to a real websockets.connect(), not worth mocking that deeply here)."""
from conftest import FakeWebSocket

from tools import termux_relay


class TestPump:
    async def test_forwards_every_message_in_order(self):
        src = FakeWebSocket(incoming=["start", b"chunk1", b"chunk2", "stop"])
        dst = FakeWebSocket()

        await termux_relay.pump(src, dst)

        assert dst.sent == ["start", b"chunk1", b"chunk2", "stop"]

    async def test_preserves_text_and_binary_framing(self):
        src = FakeWebSocket(incoming=[b"binary", "text"])
        dst = FakeWebSocket()

        await termux_relay.pump(src, dst)

        assert isinstance(dst.sent[0], (bytes, bytearray))
        assert isinstance(dst.sent[1], str)

    async def test_closes_dst_once_src_is_exhausted(self):
        src = FakeWebSocket(incoming=["only message"])
        dst = FakeWebSocket()

        await termux_relay.pump(src, dst)

        assert dst.closed is True

    async def test_empty_source_still_closes_dst(self):
        src = FakeWebSocket(incoming=[])
        dst = FakeWebSocket()

        await termux_relay.pump(src, dst)

        assert dst.sent == []
        assert dst.closed is True
