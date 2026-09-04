"""tools/echo_server.py — same wire framing as bridge_server.py but with no
STT/Ollama/VITS to mock: what goes in on "start"..."stop" comes back out
verbatim, prefixed with a "reply:(echo, N bytes)" line and terminated with
"end"."""
from conftest import FakeWebSocket

from tools import echo_server


class TestHandleClient:
    async def test_echoes_back_exactly_what_was_recorded(self):
        ws = FakeWebSocket(incoming=["start", b"abcd", b"efgh", "stop"])

        await echo_server.handle_client(ws)

        assert ws.sent[0] == "reply:(echo, 8 bytes)"
        assert ws.sent[1:-1] == [b"abcdefgh"]
        assert ws.sent[-1] == "end"

    async def test_empty_recording_still_echoes_a_zero_length_reply(self):
        ws = FakeWebSocket(incoming=["start", "stop"])

        await echo_server.handle_client(ws)

        assert ws.sent == ["reply:(echo, 0 bytes)", "end"]

    async def test_binary_before_start_is_dropped(self):
        ws = FakeWebSocket(incoming=[b"stray binary before start", "start", b"kept", "stop"])

        await echo_server.handle_client(ws)

        assert ws.sent[1:-1] == [b"kept"]

    async def test_large_recording_is_chunked_at_4000_bytes(self):
        big = b"\x00" * 8500
        ws = FakeWebSocket(incoming=["start", big, "stop"])

        await echo_server.handle_client(ws)

        audio_frames = ws.sent[1:-1]
        assert [len(f) for f in audio_frames] == [4000, 4000, 500]
        assert b"".join(audio_frames) == big

    async def test_unknown_control_message_is_ignored_not_fatal(self):
        ws = FakeWebSocket(incoming=["???", "start", b"x", "stop"])

        await echo_server.handle_client(ws)

        assert ws.sent[1:-1] == [b"x"]

    async def test_two_utterances_dont_bleed_into_each_other(self):
        ws = FakeWebSocket(incoming=["start", b"first", "stop", "start", b"second", "stop"])

        await echo_server.handle_client(ws)

        # two full reply/audio/end triplets, second utterance only has its own bytes
        assert ws.sent == [
            "reply:(echo, 5 bytes)", b"first", "end",
            "reply:(echo, 6 bytes)", b"second", "end",
        ]
