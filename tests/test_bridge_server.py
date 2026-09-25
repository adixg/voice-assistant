"""bridge_server.py's wire protocol: Session.handle_utterance() (the STT ->
Ollama -> TTS turn, with STT/LLM/TTS mocked out) and handle_client() (the
start/binary/stop/reset framing, with handle_utterance mocked out) — no real
model, no real audio device, no real network socket."""
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bridge_server
from conftest import FakeWebSocket


def make_session(voice=None):
    args = SimpleNamespace(model="rina", system="be nice", think=False)
    # spec=["ask"] keeps these on the non-streaming path: a bare Mock() would
    # auto-create ask_stream, which stream_reply would then try to iterate.
    # Streaming has its own file, tests/test_streaming.py.
    return bridge_server.Session(llm=Mock(spec=["ask"]), stt=Mock(), stt_lang="en",
                                 voice=voice, args=args)


# a couple bytes over MIN_UTTERANCE_BYTES so the length gate in
# handle_utterance() doesn't short-circuit the tests that need it to proceed
LOUD_PCM = b"\x10\x00" * (bridge_server.MIN_UTTERANCE_BYTES // 2 + 100)


class TestHandleUtterance:
    async def test_short_utterance_is_ignored(self):
        session = make_session()
        ws = FakeWebSocket()

        await session.handle_utterance(ws, b"\x00\x00")  # well under MIN_UTTERANCE_BYTES

        session.stt.transcribe.assert_not_called()
        assert ws.sent == ["end"]  # or the Stick stays on "Thinking"

    async def test_nothing_heard_sends_apology_and_no_history_change(self):
        session = make_session()
        session.stt.transcribe = Mock(return_value="")
        ws = FakeWebSocket()
        before = list(session.messages)

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent == ["reply:(didn't catch that)", "end"]
        assert session.messages == before

    async def test_normal_turn_sends_reply_then_audio_then_end(self, monkeypatch):
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/reply_0.wav"])
        session = make_session(voice=voice)
        session.stt.transcribe = Mock(return_value="hello there")
        session.llm.ask = Mock(return_value="hi, how are you")
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", AsyncMock(return_value=b"\x01\x02\x03\x04"))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent[0] == "heard:hello there"
        assert ws.sent[1] == "reply:hi, how are you"
        assert ws.sent[2:-1] == [b"\x01\x02\x03\x04"]
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
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="ok")
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", AsyncMock(return_value=big))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        audio_frames = ws.sent[2:-1]
        assert len(audio_frames) == 2
        assert audio_frames[0] == big[:bridge_server.SEND_CHUNK]
        assert audio_frames[1] == big[bridge_server.SEND_CHUNK:]

    async def test_no_voice_sends_no_audio(self):
        session = make_session(voice=None)
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="ok")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent == ["heard:hi", "reply:ok", "end"]

    async def test_llm_error_reports_and_rolls_back_history(self):
        session = make_session()
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(side_effect=RuntimeError("connection refused"))
        ws = FakeWebSocket()
        before = list(session.messages)

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent == ["heard:hi", "reply:(llm error: connection refused)", "end"]
        assert session.messages == before  # the user turn was rolled back, not left dangling


class TestEveryTurnEnds:
    """The Stick stays in its speaking state until "end" arrives, so a turn
    that fails anywhere still has to close itself off — otherwise the UI
    hangs there until the Stick is power-cycled."""

    async def test_stt_failure_still_ends_the_turn(self):
        session = make_session()
        session.stt.transcribe = Mock(side_effect=RuntimeError("whisper exploded"))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent[-1] == "end"
        assert any("whisper exploded" in m for m in ws.sent if isinstance(m, str))

    async def test_tts_failure_still_ends_the_turn(self):
        voice = Mock()
        voice.synth = Mock(side_effect=RuntimeError("worker gone"))
        session = make_session(voice=voice)
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="ok")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent[-1] == "end"

    async def test_resampler_failure_still_ends_the_turn(self, monkeypatch):
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/reply_0.wav"])
        session = make_session(voice=voice)
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="ok")
        monkeypatch.setattr(bridge_server, "resample_to_pcm16",
                            AsyncMock(side_effect=FileNotFoundError("ffmpeg")))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent[-1] == "end"

    async def test_exactly_one_end_per_successful_turn(self, monkeypatch):
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/reply_0.wav"])
        session = make_session(voice=voice)
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="ok")
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", AsyncMock(return_value=b"\x01\x02"))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, LOUD_PCM)

        assert ws.sent.count("end") == 1

    async def test_a_too_short_utterance_still_ends_the_turn(self):
        """The Stick shows "Thinking" as soon as it sends stop, so even a
        turn too short to answer needs its "end" (it used to get none and
        stayed on "Thinking" for good)."""
        session = make_session()
        ws = FakeWebSocket()

        await session.handle_utterance(ws, b"\x00\x00")

        assert ws.sent == ["end"]


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


class TestAudioNormalization:
    """The Stick's amplifier is already at full scale, so the only headroom
    left is in the signal — a measured VITS reply peaked at -3.5 dB."""

    async def test_replies_are_normalized_by_default(self, monkeypatch):
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/reply_0.wav"])
        session = make_session(voice=voice)
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="ok")
        resample = AsyncMock(return_value=b"\x01\x02")
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", resample)

        await session.handle_utterance(FakeWebSocket(), LOUD_PCM)

        resample.assert_awaited_once_with("/tmp/reply_0.wav", True)

    async def test_no_normalize_turns_it_off(self, monkeypatch):
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/reply_0.wav"])
        session = make_session(voice=voice)
        session.args.no_normalize = True
        session.normalize = False
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="ok")
        resample = AsyncMock(return_value=b"\x01\x02")
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", resample)

        await session.handle_utterance(FakeWebSocket(), LOUD_PCM)

        resample.assert_awaited_once_with("/tmp/reply_0.wav", False)

    async def test_announcements_are_normalized_too(self, monkeypatch):
        """An encouragement that comes out quieter than a reply would be an
        odd inconsistency, and it's the one nobody is waiting for."""
        voice = Mock()
        voice.synth = Mock(return_value=["/tmp/say_0.wav"])
        session = make_session(voice=voice)
        session.ws = FakeWebSocket()
        resample = AsyncMock(return_value=b"\x01\x02")
        monkeypatch.setattr(bridge_server, "resample_to_pcm16", resample)

        assert await session.announce("You've got this!") is True
        resample.assert_awaited_once_with("/tmp/say_0.wav", True)

    async def test_the_filter_is_only_added_when_asked(self, monkeypatch):
        """The filter argument must not reach ffmpeg at all with --no-normalize,
        rather than being passed as an empty -af (which ffmpeg rejects)."""
        seen = []

        async def fake_exec(*argv, **kwargs):
            seen.append(argv)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc
        monkeypatch.setattr(bridge_server.asyncio, "create_subprocess_exec", fake_exec)

        await bridge_server.resample_to_pcm16("/tmp/x.wav", normalize=True)
        await bridge_server.resample_to_pcm16("/tmp/x.wav", normalize=False)

        assert "-af" in seen[0] and bridge_server.NORMALIZE_FILTER in seen[0]
        assert "-af" not in seen[1]


class TestTheEventLoopStaysFreeDuringATurn:
    """The regression behind `keepalive ping timeout`.

    Every heavy stage of a turn is synchronous. Awaiting one inline stopped the
    event loop for its whole duration, so websockets could not answer the
    Stick's keepalive ping and tore the connection down mid-turn — the reply
    was generated and then thrown away. These tests pin the fix: the blocking
    work happens off-loop, so other tasks keep running while a turn is slow.
    """

    async def test_a_slow_turn_does_not_block_other_tasks(self):
        """A blocking stage must not stall the loop.

        A stalled loop cannot observe its own stall, so this measures it
        afterwards: a heartbeat records the gap between its own ticks, and a
        stage that runs inline shows up as one long gap. Watching a window
        "during" the block does not work — the block finishes before any
        coroutine gets to look.
        """
        import asyncio
        import time

        BLOCK = 1.0

        def slow_transcribe(*_a, **_kw):
            time.sleep(BLOCK)         # a real, thread-blocking stage
            return "hello"

        session = make_session()
        session.stt.transcribe = slow_transcribe
        session.llm.ask.return_value = "hi"
        ws = FakeWebSocket()

        gaps = []

        async def heartbeat():
            last = time.monotonic()   # stands in for websockets' ping task
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        await session.handle_utterance(ws, LOUD_PCM)
        beat.cancel()

        assert gaps, "heartbeat never ran"
        assert max(gaps) < BLOCK / 2, (
            f"the event loop stalled for {max(gaps):.2f}s during a {BLOCK}s "
            f"blocking stage — that stall is what makes websockets miss the "
            f"Stick's keepalive ping and drop the connection mid-turn")
        assert "end" in ws.sent

    async def test_iter_in_thread_preserves_order_and_completes(self):
        got = [x async for x in bridge_server.iter_in_thread(lambda: iter([1, 2, 3]))]
        assert got == [1, 2, 3]

    async def test_iter_in_thread_reraises_on_the_loop_side(self):
        def boom():
            yield 1
            raise RuntimeError("tts died")

        seen = []
        with pytest.raises(RuntimeError, match="tts died"):
            async for x in bridge_server.iter_in_thread(boom):
                seen.append(x)
        assert seen == [1]           # items before the failure still arrived

    async def test_call_in_thread_runs_off_the_event_loop(self):
        import asyncio

        loop_thread = threading.current_thread().ident
        where = await bridge_server.call_in_thread(
            lambda: threading.current_thread().ident)
        assert where != loop_thread
        assert asyncio.get_running_loop() is not None
