"""The streaming protocol: voicepipe.registry.stream_reply's fallback and
bridge_server forwarding progress to the Stick.

No network and no agent — SSE frames are fed in as captured text.
"""
from unittest.mock import AsyncMock, Mock

import bridge_server
from conftest import FakeWebSocket
from voicepipe.registry import DELTA, FINAL, STATUS, stream_reply


class TestStreamReplyFallback:
    """Callers use stream_reply so a non-streaming backend needs no shim and a
    streaming one needs no change at the call site."""

    def test_a_backend_without_ask_stream_yields_one_final(self):
        llm = Mock(spec=["ask"])
        llm.ask.return_value = "the whole answer"

        assert list(stream_reply(llm, [])) == [(FINAL, "the whole answer")]

    def test_think_is_passed_through_to_ask(self):
        llm = Mock(spec=["ask"])
        llm.ask.return_value = "x"

        list(stream_reply(llm, [{"role": "user", "content": "hi"}], think=False))

        llm.ask.assert_called_once_with([{"role": "user", "content": "hi"}], False)

    def test_a_streaming_backend_is_passed_through_verbatim(self):
        events = [(STATUS, "searching"), (DELTA, "hel"), (DELTA, "lo"), (FINAL, "hello")]
        llm = Mock()
        llm.ask_stream.return_value = iter(events)

        assert list(stream_reply(llm, [])) == events

    def test_a_declared_but_unbuilt_ask_stream_falls_back_to_ask(self):
        """A backend may declare streaming before implementing it; that must
        degrade to a working non-streaming turn, not crash a conversation."""
        llm = Mock()
        llm.ask_stream.side_effect = NotImplementedError
        llm.ask.return_value = "fallback answer"

        assert list(stream_reply(llm, [])) == [(FINAL, "fallback answer")]


class TestBridgeForwardsProgress:
    """The payoff: the Stick's caption shows the tool being run instead of a
    frozen transcript."""

    def make_session(self, llm):
        from types import SimpleNamespace
        args = SimpleNamespace(model="rina", system="be nice", think=False)
        return bridge_server.Session(llm=llm, stt=Mock(), stt_lang="en", voice=None, args=args)

    LOUD = b"\x10\x00" * (bridge_server.MIN_UTTERANCE_BYTES // 2 + 100)

    async def test_status_events_are_sent_before_the_reply(self):
        llm = Mock()
        llm.ask_stream.return_value = iter([
            (STATUS, "web search"), (STATUS, "calendar"), (FINAL, "you're free")])
        session = self.make_session(llm)
        session.stt.transcribe = Mock(return_value="am i busy")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        assert ws.sent == ["heard:am i busy", "status:web search", "status:calendar",
                           "reply:you're free", "end"]

    async def test_a_non_streaming_backend_sends_no_status(self):
        llm = Mock(spec=["ask"])
        llm.ask.return_value = "hi there"
        session = self.make_session(llm)
        session.stt.transcribe = Mock(return_value="hello")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        assert ws.sent == ["heard:hello", "reply:hi there", "end"]

    async def test_deltas_are_not_forwarded_to_the_stick(self):
        """Partial text would only make the caption flicker; the Stick gets
        whole sentences to speak."""
        llm = Mock()
        llm.ask_stream.return_value = iter([(DELTA, "par"), (DELTA, "tial"), (FINAL, "partial")])
        session = self.make_session(llm)
        session.stt.transcribe = Mock(return_value="hi")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        assert ws.sent == ["heard:hi", "reply:partial", "end"]

    async def test_a_failure_mid_stream_still_ends_the_turn(self):
        def explode(*_a, **_kw):
            yield (STATUS, "web search")
            raise RuntimeError("agent died")

        llm = Mock()
        llm.ask_stream.side_effect = explode
        session = self.make_session(llm)
        session.stt.transcribe = Mock(return_value="hi")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        assert ws.sent[-1] == "end"
        assert "status:web search" in ws.sent
