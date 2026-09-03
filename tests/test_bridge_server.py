"""bridge_server.py's wire protocol: Session.handle_utterance() (the STT ->
Ollama -> TTS turn, with STT/LLM/TTS mocked out) and handle_client() (the
start/binary/stop/reset framing, with handle_utterance mocked out) — no real
model, no real audio device, no real network socket."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bridge_server


class FakeWebSocket:
    """Stand-in for a websockets ServerConnection: async-iterable over a
    preset message sequence, and collects everything sent back."""

    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self.sent = []
        self.remote_address = ("203.0.113.5", 12345)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def send(self, msg):
        self.sent.append(msg)


def make_session(voice=None):
    args = SimpleNamespace(model="rina", system="be nice", think=False)
    return bridge_server.Session(client=Mock(), stt=Mock(), stt_lang="en", voice=voice, args=args)


# a couple bytes over MIN_UTTERANCE_BYTES so the length gate in
# handle_utterance() doesn't short-circuit the tests that need it to proceed
LOUD_PCM = b"\x10\x00" * (bridge_server.MIN_UTTERANCE_BYTES // 2 + 100)


class TestHandleUtterance:
    async def test_short_utterance_is_ignored(self, monkeypatch):
        session = make_session()
        transcribe = Mock()
        monkeypatch.setattr(bridge_server, "transcribe", transcribe)
        ws = FakeWebSocket()

        await session.handle_utterance(ws, b"\x00\x00")  # well under MIN_UTTERANCE_BYTES

        transcribe.assert_not_called()
        assert ws.sent == []

    async def test_nothing_heard_sends_apology_and_no_history_change(self, monkeypatch):
        session = make_session()
        monkeypatch.setattr(bridge_server, "transcribe", Mock(return_value=""))
        ws = FakeWebSocket()
        before = list(session.messages)

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent == ["reply:(didn't catch that)", "end"]
        assert session.messages == before

    async def test_normal_turn_sends_reply_then_audio_then_end(self, monkeypatch):
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/reply_0.wav"])
        session = make_session(voice=voice)
        monkeypatch.setattr(bridge_server, "transcribe", Mock(return_value="hello there"))
        monkeypatch.setattr(bridge_server, "ask", Mock(return_value="hi, how are you"))
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", AsyncMock(return_value=b"\x01\x02\x03\x04"))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent[0] == "reply:hi, how are you"
        assert ws.sent[1:-1] == [b"\x01\x02\x03\x04"]
        assert ws.sent[-1] == "end"
        voice.synth.assert_called_once_with("hi, how are you")
        assert session.messages[-2:] == [
            {"role": "user", "content": "hello there"},
            {"role": "assistant", "content": "hi, how are you"},
        ]

    async def test_audio_reply_is_chunked_at_send_chunk_boundary(self, monkeypatch):
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/reply_0.wav"])
        session = make_session(voice=voice)
        big = b"\x00" * (bridge_server.SEND_CHUNK + 500)
        monkeypatch.setattr(bridge_server, "transcribe", Mock(return_value="hi"))
        monkeypatch.setattr(bridge_server, "ask", Mock(return_value="ok"))
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", AsyncMock(return_value=big))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        audio_frames = ws.sent[1:-1]
        assert len(audio_frames) == 2
        assert audio_frames[0] == big[:bridge_server.SEND_CHUNK]
        assert audio_frames[1] == big[bridge_server.SEND_CHUNK:]

    async def test_no_voice_sends_no_audio(self, monkeypatch):
        session = make_session(voice=None)
        monkeypatch.setattr(bridge_server, "transcribe", Mock(return_value="hi"))
        monkeypatch.setattr(bridge_server, "ask", Mock(return_value="ok"))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent == ["reply:ok", "end"]

    async def test_ollama_error_reports_and_rolls_back_history(self, monkeypatch):
        session = make_session()
        monkeypatch.setattr(bridge_server, "transcribe", Mock(return_value="hi"))
        monkeypatch.setattr(bridge_server, "ask", Mock(side_effect=RuntimeError("connection refused")))
        ws = FakeWebSocket()
        before = list(session.messages)

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent == ["reply:(ollama error: connection refused)", "end"]
        assert session.messages == before  # the user turn was rolled back, not left dangling


class TestHandleClient:
    async def test_start_stop_dispatches_one_utterance_with_buffered_audio(self, monkeypatch):
        session = make_session()
        handle_utterance = AsyncMock()
        monkeypatch.setattr(session, "handle_utterance", handle_utterance)
        ws = FakeWebSocket(incoming=["start", b"abcd", b"efgh", "stop"])

        await bridge_server.handle_client(ws, session)

        handle_utterance.assert_awaited_once_with(ws, b"abcdefgh")

    async def test_binary_before_start_is_dropped(self, monkeypatch):
        session = make_session()
        handle_utterance = AsyncMock()
        monkeypatch.setattr(session, "handle_utterance", handle_utterance)
        ws = FakeWebSocket(incoming=[b"stray binary before start", "start", b"kept", "stop"])

        await bridge_server.handle_client(ws, session)

        handle_utterance.assert_awaited_once_with(ws, b"kept")

    async def test_reset_clears_history_but_keeps_system_prompt(self):
        session = make_session()
        session.messages.append({"role": "user", "content": "old question"})
        session.messages.append({"role": "assistant", "content": "old answer"})
        ws = FakeWebSocket(incoming=["reset"])

        await bridge_server.handle_client(ws, session)

        assert session.messages == [{"role": "system", "content": "be nice"}]

    async def test_unknown_control_message_is_ignored_not_fatal(self, monkeypatch):
        session = make_session()
        handle_utterance = AsyncMock()
        monkeypatch.setattr(session, "handle_utterance", handle_utterance)
        ws = FakeWebSocket(incoming=["???", "start", b"x", "stop"])

        await bridge_server.handle_client(ws, session)

        handle_utterance.assert_awaited_once_with(ws, b"x")
