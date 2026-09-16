"""services/gateway/app.py -- Phase 1 skeleton. Tests run_turn() (the
stt -> agent -> tts chain) directly against a mocked httpx transport, rather
than through the /turn websocket, so they check the actual HTTP calls made
to each downstream service without needing stt/agent/tts running for real.

See the module's own docstring for what this gateway does *not* yet do
(firmware wire protocol, speaker gate, announce) -- not covered here because
it doesn't exist yet.
"""
import base64
import json

import httpx
import pytest

import services.gateway.app as gateway_app


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def urls(monkeypatch):
    monkeypatch.setattr(gateway_app, "_urls", {
        "stt": "http://stt",
        "agent": "http://agent",
        "tts": "http://tts",
    })
    return gateway_app._urls


class TestRunTurn:
    async def test_chains_stt_then_agent_then_tts_in_order(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/transcribe":
                return httpx.Response(200, json={"text": "turn on the lights"})
            if request.url.path == "/ask":
                body = json.loads(request.content)
                assert body["messages"][-1] == {"role": "user", "content": "turn on the lights"}
                return httpx.Response(200, json={"reply": "done"})
            if request.url.path == "/synth":
                body = json.loads(request.content)
                assert body["text"] == "done"
                chunk = base64.b64encode(b"audio-bytes").decode("ascii")
                return httpx.Response(200, json={"chunks_b64": [chunk]})
            raise AssertionError(f"unexpected request to {request.url.path}")

        async with make_client(handler) as client:
            result = await gateway_app.run_turn(client, b"fake wav bytes", messages=[])

        assert calls == ["/transcribe", "/ask", "/synth"]
        assert result == {
            "heard": "turn on the lights",
            "reply": "done",
            "chunks_b64": [base64.b64encode(b"audio-bytes").decode("ascii")],
        }

    async def test_appends_the_transcript_to_existing_message_history(self):
        seen_messages = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/transcribe":
                return httpx.Response(200, json={"text": "and then?"})
            if request.url.path == "/ask":
                seen_messages["messages"] = json.loads(request.content)["messages"]
                return httpx.Response(200, json={"reply": "ok"})
            if request.url.path == "/synth":
                return httpx.Response(200, json={"chunks_b64": []})
            raise AssertionError(request.url.path)

        history = [{"role": "system", "content": "be helpful"}]
        async with make_client(handler) as client:
            await gateway_app.run_turn(client, b"wav", messages=history)

        assert seen_messages["messages"] == [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "and then?"},
        ]

    async def test_raises_if_a_downstream_service_errors(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        async with make_client(handler) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await gateway_app.run_turn(client, b"wav", messages=[])
