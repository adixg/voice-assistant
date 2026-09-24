"""services/agent/app.py -- the LLM registry served over HTTP, including the
streaming endpoint's fallback behaviour for a backend with no ask_stream
(see voicepipe.registry.stream_reply, which this reuses rather than
reimplementing)."""
import json

from fastapi.testclient import TestClient

import services.agent.app as agent_app


class FakeLLM:
    """No ask_stream -- exercises stream_reply()'s fallback-to-ask() path,
    same as a plain (non-agentic) backend like ollama.py."""

    def __init__(self, reply="a plain reply"):
        self.reply = reply
        self.calls = []

    def ask(self, messages, think=None):
        self.calls.append((messages, think))
        return self.reply


class StreamingFakeLLM(FakeLLM):
    def ask_stream(self, messages, think=None):
        yield "status", "thinking..."
        yield "delta", "partial "
        yield "final", "partial reply"


class TestHealth:
    def test_reports_ok_with_no_backend(self, monkeypatch):
        monkeypatch.setattr(agent_app, "_backend", None)
        client = TestClient(agent_app.app)

        assert client.get("/health").json() == {"status": "ok", "backend": None}


class TestAsk:
    def test_returns_503_when_backend_not_ready(self, monkeypatch):
        monkeypatch.setattr(agent_app, "_backend", None)
        client = TestClient(agent_app.app)

        resp = client.post("/ask", json={"messages": [{"role": "user", "content": "hi"}]})

        assert resp.status_code == 503

    def test_forwards_messages_and_think_and_returns_the_reply(self, monkeypatch):
        fake = FakeLLM(reply="hello back")
        monkeypatch.setattr(agent_app, "_backend", fake)
        client = TestClient(agent_app.app)

        resp = client.post("/ask", json={
            "messages": [{"role": "user", "content": "hi"}],
            "think": False,
        })

        assert resp.status_code == 200
        assert resp.json() == {"reply": "hello back"}
        assert fake.calls == [([{"role": "user", "content": "hi"}], False)]


class TestAskStream:
    def test_falls_back_to_a_single_final_event_with_no_ask_stream(self, monkeypatch):
        monkeypatch.setattr(agent_app, "_backend", FakeLLM(reply="whole reply at once"))
        client = TestClient(agent_app.app)

        resp = client.post("/ask_stream", json={"messages": [{"role": "user", "content": "hi"}]})

        events = [json.loads(line) for line in resp.text.splitlines() if line]
        assert events == [{"kind": "final", "text": "whole reply at once"}]

    def test_streams_every_event_a_streaming_backend_yields(self, monkeypatch):
        monkeypatch.setattr(agent_app, "_backend", StreamingFakeLLM())
        client = TestClient(agent_app.app)

        resp = client.post("/ask_stream", json={"messages": [{"role": "user", "content": "hi"}]})

        events = [json.loads(line) for line in resp.text.splitlines() if line]
        assert events == [
            {"kind": "status", "text": "thinking..."},
            {"kind": "delta", "text": "partial "},
            {"kind": "final", "text": "partial reply"},
        ]


class TestLlmTargetMetric:
    def test_reports_the_server_the_agent_calls_and_replaces_the_old_one(self):
        from prometheus_client import REGISTRY
        from services.metrics import set_agent_llm_target

        set_agent_llm_target("http://llama-cpp-gtx1650:8080/v1", "qwen3.5-4b")
        set_agent_llm_target("http://llama-cpp-rtx4060:8080/v1", "qwen3-8b")

        get = lambda host: REGISTRY.get_sample_value(  # noqa: E731
            "aicompanion_agent_llm_target_info", {"host": host, "model": "qwen3-8b" if "4060" in host else "qwen3.5-4b"})
        assert get("http://llama-cpp-rtx4060:8080/v1") == 1
        assert get("http://llama-cpp-gtx1650:8080/v1") is None
