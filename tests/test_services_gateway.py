"""services/gateway/app.py's wire protocol: GatewaySession.handle_utterance()
(the stt -> agent -> tts turn, each a mocked HTTP call, plus the in-process
speaker gate) and handle_client() (the start/binary/stop/reset framing) --
mirrors tests/test_bridge_server.py's structure closely, since this is the
same protocol over HTTP-backed stages instead of in-process ones.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.routing import WebSocketRoute

import services.gateway.app as gateway_app
from conftest import FakeWebSocket
from voicepipe.speaker import CHECK_FAILED_LINES


def make_args(**overrides):
    defaults = dict(system="be nice", think=False, no_voice=False, no_normalize=False,
                    stt_lang="en")
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_session(client=None, gate=None, args=None):
    urls = {"stt": "http://stt", "agent": "http://agent", "tts": "http://tts"}
    return gateway_app.GatewaySession(client, urls, gate, args or make_args())


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ndjson_stream_handler(reply, statuses=()):
    """A /ask_stream responder: zero or more status events, then one final."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ask_stream":
            lines = [json.dumps({"kind": "status", "text": s}) for s in statuses]
            lines.append(json.dumps({"kind": "final", "text": reply}))
            return httpx.Response(200, content="\n".join(lines) + "\n")
        raise AssertionError(f"unexpected request to {request.url.path}")
    return handler


# a couple bytes over MIN_UTTERANCE_BYTES so the length gate in
# handle_utterance() doesn't short-circuit the tests that need it to proceed
LOUD_PCM = b"\x10\x00" * (gateway_app.MIN_UTTERANCE_BYTES // 2 + 100)


def stt_agent_tts_handler(text="hello there", reply="hi, how are you", chunks=(b"\x01\x02\x03\x04",)):
    import base64

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transcribe":
            return httpx.Response(200, json={"text": text})
        if request.url.path == "/ask_stream":
            return httpx.Response(200, content=json.dumps({"kind": "final", "text": reply}) + "\n")
        if request.url.path == "/synth":
            chunks_b64 = [base64.b64encode(c).decode("ascii") for c in chunks]
            return httpx.Response(200, json={"chunks_b64": chunks_b64})
        raise AssertionError(f"unexpected request to {request.url.path}")
    return handler


class TestHandleUtterance:
    async def test_short_utterance_is_ignored(self, monkeypatch):
        async with make_client(stt_agent_tts_handler()) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, b"\x00\x00")  # well under MIN_UTTERANCE_BYTES

            assert ws.sent == []

    async def test_nothing_heard_sends_apology_and_no_history_change(self):
        async with make_client(stt_agent_tts_handler(text="")) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()
            before = list(session.messages)

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent == ["reply:(didn't catch that)", "end"]
            assert session.messages == before

    async def test_normal_turn_sends_heard_then_reply_then_audio_then_end(self, monkeypatch):
        monkeypatch.setattr(gateway_app, "resample_to_pcm16", AsyncMock(return_value=b"\x01\x02\x03\x04"))
        async with make_client(stt_agent_tts_handler()) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[0] == "heard:hello there"
            assert ws.sent[1] == "reply:hi, how are you"
            assert ws.sent[2:-1] == [b"\x01\x02\x03\x04"]
            assert ws.sent[-1] == "end"
            assert session.messages[-2:] == [
                {"role": "user", "content": "hello there"},
                {"role": "assistant", "content": "hi, how are you"},
            ]

    async def test_status_events_are_forwarded_before_the_reply(self):
        stream_handler = ndjson_stream_handler("done", statuses=["searching the web"])

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/transcribe":
                return httpx.Response(200, json={"text": "turn on the lights"})
            if request.url.path == "/synth":
                return httpx.Response(200, json={"chunks_b64": []})
            return stream_handler(request)

        async with make_client(handler) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent == [
                "heard:turn on the lights",
                "status:searching the web",
                "reply:done",
                "end",
            ]

    async def test_audio_reply_is_chunked_at_send_chunk_boundary(self, monkeypatch):
        big = b"\x00" * (gateway_app.SEND_CHUNK + 500)
        monkeypatch.setattr(gateway_app, "resample_to_pcm16", AsyncMock(return_value=big))
        async with make_client(stt_agent_tts_handler(text="hi", reply="ok")) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            audio_frames = ws.sent[2:-1]
            assert len(audio_frames) == 2
            assert audio_frames[0] == big[:gateway_app.SEND_CHUNK]
            assert audio_frames[1] == big[gateway_app.SEND_CHUNK:]

    async def test_no_voice_sends_no_audio(self):
        async with make_client(stt_agent_tts_handler(text="hi", reply="ok")) as client:
            session = make_session(client=client, args=make_args(no_voice=True))
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent == ["heard:hi", "reply:ok", "end"]

    async def test_agent_error_reports_and_rolls_back_history(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/transcribe":
                return httpx.Response(200, json={"text": "hi"})
            if request.url.path == "/ask_stream":
                return httpx.Response(500, text="connection refused")
            raise AssertionError(request.url.path)

        async with make_client(handler) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()
            before = list(session.messages)

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[0] == "heard:hi"
            assert "llm error" in ws.sent[1]
            assert ws.sent[-1] == "end"
            assert session.messages == before  # the user turn was rolled back, not left dangling


class TestEveryTurnEnds:
    """Same invariant as bridge_server.py's Session: the Stick stays in its
    speaking state until "end" arrives, so a turn that fails anywhere still
    has to close itself off."""

    async def test_stt_failure_still_ends_the_turn(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="whisper exploded")

        async with make_client(handler) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[-1] == "end"

    async def test_tts_failure_still_ends_the_turn(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/transcribe":
                return httpx.Response(200, json={"text": "hi"})
            if request.url.path == "/ask_stream":
                return httpx.Response(200, content=json.dumps({"kind": "final", "text": "ok"}) + "\n")
            if request.url.path == "/synth":
                return httpx.Response(500, text="worker gone")
            raise AssertionError(request.url.path)

        async with make_client(handler) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[-1] == "end"

    async def test_exactly_one_end_per_successful_turn(self, monkeypatch):
        monkeypatch.setattr(gateway_app, "resample_to_pcm16", AsyncMock(return_value=b"\x01\x02"))
        async with make_client(stt_agent_tts_handler(text="hi", reply="ok")) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent.count("end") == 1

    async def test_a_too_short_utterance_sends_nothing_at_all(self):
        async with make_client(stt_agent_tts_handler()) as client:
            session = make_session(client=client)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, b"\x00\x00")

            assert ws.sent == []


class TestSpeakerGate:
    """GatewaySession._turn checks the gate before spending anything on
    stt/agent/tts, same ordering and same verdicts as bridge_server.py's
    Session._turn -- see voicepipe/speaker.py for what ACCEPTED/REJECTED/
    TOO_SHORT actually mean."""

    def make_gate(self, verdict, score=0.5):
        gate = Mock()
        gate.check = Mock(return_value=(verdict, score))
        gate.threshold = 0.6
        gate.status = Mock(return_value={"enabled": True, "ready": True, "on_error": "reject", "error": None})
        return gate

    async def test_rejected_speaks_the_rejection_line_and_skips_stt(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/synth":
                return httpx.Response(200, json={"chunks_b64": []})
            raise AssertionError(f"unexpected request to {request.url.path}")

        async with make_client(handler) as client:
            gate = self.make_gate(gateway_app.REJECTED)
            session = make_session(client=client, gate=gate)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[0].startswith("reply:")
            assert ws.sent[-1] == "end"
            assert session.rejection_streak == 1

    async def test_too_short_asks_to_repeat_and_skips_stt(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/synth":
                return httpx.Response(200, json={"chunks_b64": []})
            raise AssertionError(f"unexpected request to {request.url.path}")

        async with make_client(handler) as client:
            gate = self.make_gate(gateway_app.TOO_SHORT)
            session = make_session(client=client, gate=gate)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[0].startswith("reply:")
            assert ws.sent[-1] == "end"
            assert session.rejection_streak == 0  # not held against them

    async def test_a_broken_check_refuses_neutrally_and_skips_stt(self):
        """Regression: the gateway image had no curl, so the check failed on every
        utterance and the old code let everyone through."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/synth":
                return httpx.Response(200, json={"chunks_b64": []})
            raise AssertionError(f"unexpected request to {request.url.path}")

        async with make_client(handler) as client:
            gate = self.make_gate(gateway_app.CHECK_FAILED, score=None)
            session = make_session(client=client, gate=gate)
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[0].startswith("reply:") and ws.sent[-1] == "end"
            assert ws.sent[0][len("reply:"):] in CHECK_FAILED_LINES
            assert session.rejection_streak == 0

    async def test_accepted_proceeds_to_stt_and_resets_the_streak(self):
        async with make_client(stt_agent_tts_handler(text="hi", reply="ok")) as client:
            gate = self.make_gate("accepted")
            session = make_session(client=client, gate=gate)
            session.rejection_streak = 3
            ws = FakeWebSocket()

            await session.handle_utterance(ws, LOUD_PCM)

            assert ws.sent[0] == "heard:hi"
            assert session.rejection_streak == 0


class TestHandleClient:
    async def test_start_stop_dispatches_one_utterance_with_buffered_audio(self, monkeypatch):
        session = make_session()
        handle_utterance = AsyncMock()
        monkeypatch.setattr(session, "handle_utterance", handle_utterance)
        ws = FakeWebSocket(incoming=["start", b"abcd", b"efgh", "stop"])

        await gateway_app.handle_client(ws, session)

        handle_utterance.assert_awaited_once_with(ws, b"abcdefgh")

    async def test_binary_before_start_is_dropped(self, monkeypatch):
        session = make_session()
        handle_utterance = AsyncMock()
        monkeypatch.setattr(session, "handle_utterance", handle_utterance)
        ws = FakeWebSocket(incoming=[b"stray binary before start", "start", b"kept", "stop"])

        await gateway_app.handle_client(ws, session)

        handle_utterance.assert_awaited_once_with(ws, b"kept")

    async def test_reset_clears_history_but_keeps_system_prompt(self):
        session = make_session()
        session.messages.append({"role": "user", "content": "old question"})
        session.messages.append({"role": "assistant", "content": "old answer"})
        ws = FakeWebSocket(incoming=["reset"])

        await gateway_app.handle_client(ws, session)

        assert session.messages == [{"role": "system", "content": "be nice"}]

    async def test_unknown_control_message_is_ignored_not_fatal(self, monkeypatch):
        session = make_session()
        handle_utterance = AsyncMock()
        monkeypatch.setattr(session, "handle_utterance", handle_utterance)
        ws = FakeWebSocket(incoming=["???", "start", b"x", "stop"])

        await gateway_app.handle_client(ws, session)

        handle_utterance.assert_awaited_once_with(ws, b"x")

    async def test_session_ws_is_set_during_the_connection_and_cleared_after(self):
        session = make_session()
        ws = FakeWebSocket(incoming=[])

        await gateway_app.handle_client(ws, session)

        assert session.ws is None  # cleared once the connection ends


class TestWebSocketRoutes:
    def test_exposes_relay_root_and_legacy_stick_paths(self):
        paths = {route.path for route in gateway_app.app.routes
                 if isinstance(route, WebSocketRoute)}
        assert {"/", "/stick"} <= paths


class TestAnnounce:
    async def test_no_stick_connected_returns_false(self):
        session = make_session()
        assert await session.announce("hi") is False

    async def test_announce_sends_reply_audio_then_end(self, monkeypatch):
        monkeypatch.setattr(gateway_app, "resample_to_pcm16", AsyncMock(return_value=b"\x01\x02"))
        async with make_client(lambda r: httpx.Response(200, json={"chunks_b64": [
            __import__("base64").b64encode(b"wav").decode()]})) as client:
            session = make_session(client=client)
            session.ws = FakeWebSocket()

            assert await session.announce("You've got this!") is True
            assert session.ws.sent == ["reply:You've got this!", b"\x01\x02", "end"]

    async def test_no_normalize_is_passed_through_to_resample(self, monkeypatch):
        resample = AsyncMock(return_value=b"\x01\x02")
        monkeypatch.setattr(gateway_app, "resample_to_pcm16", resample)
        async with make_client(lambda r: httpx.Response(200, json={"chunks_b64": [
            __import__("base64").b64encode(b"wav").decode()]})) as client:
            session = make_session(client=client, args=make_args(no_normalize=True))
            session.normalize = False
            session.ws = FakeWebSocket()

            await session.announce("hi")

            assert resample.await_args.args[1] is False


class TestHealthReportsWhetherTheGateActuallyWorks:
    def test_gate_true_alone_used_to_hide_a_gate_that_could_not_run(self, monkeypatch):
        from fastapi.testclient import TestClient
        gate = Mock()
        gate.enabled = True
        gate.status = Mock(return_value={"enabled": True, "ready": False, "on_error": "reject",
                                         "error": "couldn't download the speaker model"})
        session = SimpleNamespace(gate=gate)
        monkeypatch.setattr(gateway_app, "_session", session)
        monkeypatch.setattr(gateway_app, "_urls", {})

        body = TestClient(gateway_app.app).get("/health").json()

        assert body["gate"] is True                       # configured...
        assert body["speaker_gate"]["ready"] is False     # ...but not working, and now visible
        assert "download" in body["speaker_gate"]["error"]

    def test_no_gate_reports_none(self, monkeypatch):
        from fastapi.testclient import TestClient
        monkeypatch.setattr(gateway_app, "_session", SimpleNamespace(gate=None))
        monkeypatch.setattr(gateway_app, "_urls", {})
        body = TestClient(gateway_app.app).get("/health").json()
        assert body["gate"] is False and body["speaker_gate"] is None


# ------------------------------------------------- speaker verdicts are recorded
from prometheus_client import REGISTRY  # noqa: E402


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class TestSpeakerVerdictsAreRecorded:
    """Every verdict is logged and counted, so acceptances are on the record and
    the "let through unchecked" hole shows up as its own series."""

    def gate(self, verdict, score, ready=True):
        gate = Mock()
        gate.check = Mock(return_value=(verdict, score))
        gate.threshold = 0.6
        gate.status = Mock(return_value={"enabled": True, "ready": ready, "on_error": "reject",
                                         "error": None if ready else "no model"})
        return gate

    async def run_turn(self, gate):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/transcribe":
                return httpx.Response(200, json={"text": "hi"})
            if request.url.path == "/ask_stream":
                return httpx.Response(200, content=json.dumps({"kind": "final", "text": "ok"}) + "\n")
            return httpx.Response(200, json={"chunks_b64": []})
        async with make_client(handler) as client:
            await make_session(client=client, gate=gate).handle_utterance(FakeWebSocket(), LOUD_PCM)

    async def test_a_scored_acceptance_is_counted_scored_and_logged(self, capsys):
        before = sample("aicompanion_gateway_speaker_checks_total", verdict="accepted")
        n_before = sample("aicompanion_gateway_speaker_score_count", verdict="accepted")
        await self.run_turn(self.gate("accepted", 0.61))

        assert sample("aicompanion_gateway_speaker_checks_total", verdict="accepted") == before + 1
        assert sample("aicompanion_gateway_speaker_score_count", verdict="accepted") == n_before + 1
        assert sample("aicompanion_gateway_speaker_last_score") == 0.61
        assert "speaker verdict=accepted score=0.610 threshold=0.6" in capsys.readouterr().out

    async def test_an_acceptance_without_a_score_is_counted_as_unverified_not_accepted(self, capsys):
        """Gate off, or a short clip let through by --short-utterances allow."""
        accepted = sample("aicompanion_gateway_speaker_checks_total", verdict="accepted")
        unverified = sample("aicompanion_gateway_speaker_checks_total", verdict="unverified")
        await self.run_turn(self.gate("accepted", None))

        assert sample("aicompanion_gateway_speaker_checks_total", verdict="unverified") == unverified + 1
        assert sample("aicompanion_gateway_speaker_checks_total", verdict="accepted") == accepted
        assert "verdict=unverified score=-" in capsys.readouterr().out

    @pytest.mark.parametrize("verdict", ["rejected", "too_short", "check_failed"])
    async def test_every_other_verdict_is_counted_and_logged(self, verdict, capsys):
        before = sample("aicompanion_gateway_speaker_checks_total", verdict=verdict)
        await self.run_turn(self.gate(verdict, 0.08 if verdict == "rejected" else None))

        assert sample("aicompanion_gateway_speaker_checks_total", verdict=verdict) == before + 1
        assert f"verdict={verdict}" in capsys.readouterr().out

    async def test_the_gate_state_gauges_follow_the_gate(self):
        await self.run_turn(self.gate("check_failed", None, ready=False))
        assert sample("aicompanion_gateway_speaker_gate_enabled") == 1
        assert sample("aicompanion_gateway_speaker_gate_ready") == 0      # enabled but cannot run
        assert sample("aicompanion_gateway_speaker_threshold") == 0.6

        await self.run_turn(self.gate("accepted", 0.9, ready=True))
        assert sample("aicompanion_gateway_speaker_gate_ready") == 1

    async def test_no_verdict_is_recorded_when_there_is_no_gate(self):
        total = lambda: sum(sample("aicompanion_gateway_speaker_checks_total", verdict=v)  # noqa: E731
                            for v in ("accepted", "rejected", "too_short", "check_failed", "unverified"))
        before = total()
        await self.run_turn(None)
        assert total() == before

    def test_every_verdict_series_exists_at_zero_from_the_start(self):
        """Otherwise increase() over a window misses the very first event."""
        for verdict in ("accepted", "rejected", "too_short", "check_failed", "unverified"):
            assert REGISTRY.get_sample_value("aicompanion_gateway_speaker_checks_total",
                                             {"verdict": verdict}) is not None
