"""voicepipe.encouragement — the unprompted lines, and bridge_server's loop
that speaks them. No timers are actually waited on: asyncio.sleep is patched
out, so the whole schedule is exercised in milliseconds."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bridge_server
from voicepipe import encouragement
from voicepipe.speaker import OWNER


@pytest.fixture(autouse=True)
def _fresh_history():
    encouragement.reset()
    yield
    encouragement.reset()


class TestLines:
    def test_every_line_is_non_empty(self):
        assert encouragement.LINES
        assert all(t.strip() for t in encouragement.LINES)

    def test_the_owner_placeholder_is_always_filled_in(self):
        """A stray {owner} would be read out literally by the TTS."""
        for _ in range(len(encouragement.LINES) * 2):
            text = encouragement.line()
            assert "{" not in text and "}" not in text

    def test_the_owners_name_is_not_hardcoded(self):
        """It comes from speaker.OWNER, so renaming happens in one place."""
        assert all(OWNER not in t for t in encouragement.LINES)

    def test_the_owner_can_be_overridden(self):
        seen = {encouragement.line(owner="Kaito") for _ in range(len(encouragement.LINES))}
        assert any("Kaito" in t for t in seen)

    def test_no_line_carries_stage_directions_or_emoji(self):
        """Same rule as the personas: the TTS reads these aloud."""
        for text in encouragement.LINES:
            assert "*" not in text
            assert text.isascii(), text

    def test_recent_lines_are_not_repeated(self):
        window = encouragement.NO_REPEAT_WINDOW
        drawn = [encouragement.line() for _ in range(window)]
        assert len(set(drawn)) == window

    def test_it_keeps_working_past_the_whole_list(self):
        """Once every line is 'recent' it must fall back, not raise or stall."""
        for _ in range(len(encouragement.LINES) * 3):
            assert encouragement.line()


class TestEncourageLoop:
    def _session(self, announce):
        return SimpleNamespace(announce=announce)

    async def _run_ticks(self, session, monkeypatch, ticks):
        """Let the loop fire exactly `ticks` times, then stop it."""
        calls = {"n": 0}

        async def fake_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] > ticks:
                raise asyncio.CancelledError
        monkeypatch.setattr(bridge_server.asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await bridge_server.encourage_loop(session, 15, 20)

    async def test_it_announces_on_each_tick(self, monkeypatch):
        announce = AsyncMock(return_value=True)
        await self._run_ticks(self._session(announce), monkeypatch, ticks=3)
        assert announce.await_count == 3

    async def test_each_interval_is_drawn_from_the_range(self, monkeypatch):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            if len(slept) > 20:
                raise asyncio.CancelledError
        monkeypatch.setattr(bridge_server.asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await bridge_server.encourage_loop(self._session(AsyncMock(return_value=True)), 15, 20)

        assert all(15 * 60 <= s <= 20 * 60 for s in slept)
        assert len(set(slept)) > 1  # drawn fresh, not a fixed schedule

    async def test_no_stick_connected_is_not_fatal(self, monkeypatch):
        """announce() returns False when nothing is listening; the loop keeps
        running so it works again once the Stick reconnects."""
        announce = AsyncMock(return_value=False)
        await self._run_ticks(self._session(announce), monkeypatch, ticks=2)
        assert announce.await_count == 2

    async def test_a_failed_announcement_does_not_kill_the_loop(self, monkeypatch):
        announce = AsyncMock(side_effect=[RuntimeError("tts worker gone"), True])
        await self._run_ticks(self._session(announce), monkeypatch, ticks=2)
        assert announce.await_count == 2


class TestAnnouncementsDoNotInterleave:
    """`reply:` / audio / `end` is a frame sequence. An announcement that
    starts mid-turn would interleave its PCM with the reply's, and the Stick
    would play both as noise."""

    def _session(self):
        args = SimpleNamespace(model="rina", system="be nice", think=False)
        return bridge_server.Session(llm=Mock(spec=["ask"]), stt=Mock(), stt_lang="en",
                                     voice=None, args=args)

    async def test_announce_waits_for_a_turn_in_flight(self, monkeypatch):
        from conftest import FakeWebSocket

        session = self._session()
        ws = FakeWebSocket()
        session.ws = ws
        session.stt.transcribe = Mock(return_value="hi")

        released = asyncio.Event()

        async def slow_ask(_ws):
            await released.wait()
            return "the answer"
        monkeypatch.setattr(session, "_ask", slow_ask)

        pcm = b"\x10\x00" * (bridge_server.MIN_UTTERANCE_BYTES // 2 + 100)
        turn = asyncio.create_task(session.handle_utterance(ws, pcm))
        await asyncio.sleep(0)
        shout = asyncio.create_task(session.announce("You've got this!"))
        await asyncio.sleep(0)

        assert "reply:You've got this!" not in ws.sent  # blocked behind the turn
        released.set()
        await asyncio.gather(turn, shout)

        # The turn completed in one piece, and the announcement came after it.
        assert ws.sent.index("reply:the answer") < ws.sent.index("end")
        assert ws.sent.index("end") < ws.sent.index("reply:You've got this!")

    async def test_announce_gives_up_if_the_stick_left_while_waiting(self):
        session = self._session()
        session.ws = Mock()
        await session.speaking.acquire()

        shout = asyncio.create_task(session.announce("hello?"))
        await asyncio.sleep(0)
        session.ws = None            # disconnected mid-wait
        session.speaking.release()

        assert await shout is False
