"""The streaming protocol: voicepipe.registry.stream_reply's fallback, the
Hermes SSE parser, and bridge_server forwarding progress to the Stick.

No network and no agent — SSE frames are fed in as captured text.
"""
from unittest.mock import AsyncMock, Mock

import pytest

import bridge_server
from conftest import FakeWebSocket
from voicepipe.backends.hermes_agent import (
    HermesAgentLLM, _delta_text, _is_progress_event, _progress_label,
)
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


class TestSSEFrameHelpers:
    def test_openai_chunk_yields_its_delta_text(self):
        assert _delta_text({"choices": [{"delta": {"content": "hi"}}]}) == "hi"

    @pytest.mark.parametrize("payload", [
        {},                                            # empty frame
        {"choices": []},                               # no choices
        {"choices": [{}]},                             # no delta
        {"choices": [{"delta": {}}]},                  # empty delta
        {"choices": [{"delta": {"content": ""}}]},     # end-of-stream empty string
        {"choices": [{"delta": {"content": None}}]},   # null content
        {"choices": "not a list"},                     # malformed
    ])
    def test_frames_with_no_text_yield_nothing(self, payload):
        assert _delta_text(payload) is None

    def test_progress_is_recognised_from_the_event_name(self):
        assert _is_progress_event("hermes.tool.progress", {})
        assert _is_progress_event("tool.started", {})

    def test_ordinary_chunks_are_not_progress(self):
        assert not _is_progress_event(None, {"choices": [{"delta": {"content": "hi"}}]})
        assert not _is_progress_event("message", {})

    def test_tool_name_becomes_the_label(self):
        assert _progress_label({"tool_name": "web_search", "delta": ""}) == "web search"

    def test_the_agents_own_thinking_step_reads_as_thinking(self):
        assert _progress_label({"tool_name": "_thinking"}) == "thinking"

    def test_label_falls_back_through_other_fields(self):
        assert _progress_label({"message": "reading calendar"}) == "reading calendar"

    def test_no_usable_label_yields_none(self):
        assert _progress_label({"unrelated": 1}) is None


def sse(*frames):
    """A fake httpx streaming response over the given raw SSE lines."""
    response = Mock()
    response.iter_lines.return_value = iter(frames)
    response.raise_for_status = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def streaming_backend(*frames):
    client = Mock()
    client.stream.return_value = sse(*frames)
    return HermesAgentLLM(client=client)


class TestHermesAskStream:
    def test_deltas_accumulate_into_the_final_reply(self):
        llm = streaming_backend(
            'data: {"choices":[{"delta":{"content":"Hello"}}]}', "",
            'data: {"choices":[{"delta":{"content":" there"}}]}', "",
            "data: [DONE]",
        )

        assert list(llm.ask_stream([])) == [
            (DELTA, "Hello"), (DELTA, " there"), (FINAL, "Hello there")]

    def test_tool_progress_becomes_a_status_event(self):
        llm = streaming_backend(
            "event: hermes.tool.progress",
            'data: {"tool_name":"web_search","delta":""}', "",
            'data: {"choices":[{"delta":{"content":"done"}}]}', "",
            "data: [DONE]",
        )

        assert list(llm.ask_stream([])) == [
            (STATUS, "web search"), (DELTA, "done"), (FINAL, "done")]

    def test_a_reasoning_block_is_stripped_from_the_final(self):
        llm = streaming_backend(
            'data: {"choices":[{"delta":{"content":"<think>hmm</think>"}}]}', "",
            'data: {"choices":[{"delta":{"content":"answer"}}]}', "",
            "data: [DONE]",
        )

        assert list(llm.ask_stream([]))[-1] == (FINAL, "answer")

    def test_a_stream_with_no_content_still_ends_with_a_final(self):
        llm = streaming_backend("data: [DONE]")
        assert list(llm.ask_stream([])) == [(FINAL, "")]

    def test_malformed_json_is_skipped_not_fatal(self):
        llm = streaming_backend(
            "data: {not json", "",
            'data: {"choices":[{"delta":{"content":"ok"}}]}', "",
            "data: [DONE]",
        )

        assert list(llm.ask_stream([])) == [(DELTA, "ok"), (FINAL, "ok")]

    def test_unknown_event_types_are_ignored(self):
        llm = streaming_backend(
            "event: something.new", 'data: {"whatever":1}', "",
            'data: {"choices":[{"delta":{"content":"ok"}}]}', "",
            "data: [DONE]",
        )

        assert list(llm.ask_stream([])) == [(DELTA, "ok"), (FINAL, "ok")]

    def test_it_requests_a_stream(self):
        llm = streaming_backend("data: [DONE]")
        list(llm.ask_stream([{"role": "user", "content": "hi"}]))

        kwargs = llm.client.stream.call_args[1]
        assert kwargs["json"]["stream"] is True

    def test_a_missing_event_line_still_detects_progress_from_the_body(self):
        """Not every server writes the `event:` line; the frame body can say so."""
        llm = streaming_backend(
            'data: {"object":"tool.progress","tool_name":"calendar"}', "",
            "data: [DONE]",
        )

        assert (STATUS, "calendar") in list(llm.ask_stream([]))


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
