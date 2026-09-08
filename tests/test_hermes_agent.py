"""voicepipe.backends.hermes_agent — the OpenAI-compatible client that puts a
full agent behind the LLM slot. No running agent, no network: the httpx client
is injected."""
from unittest.mock import Mock

import pytest

from voicepipe.backends.hermes_agent import (
    DEFAULT_MODEL, DEFAULT_TIMEOUT, DEFAULT_URL, HermesAgentLLM, HermesAgentUnavailable,
)


def reply(content):
    """A minimal OpenAI chat-completions response."""
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


def backend(post_response=None, **kwargs):
    client = Mock()
    if post_response is not None:
        client.post.return_value = post_response
    return HermesAgentLLM(client=client, **kwargs), client


class TestAsk:
    def test_returns_the_assistant_content(self):
        llm, _ = backend(reply("the sun is out"))
        assert llm.ask([{"role": "user", "content": "weather?"}]) == "the sun is out"

    def test_posts_the_messages_and_model_unchanged(self):
        llm, client = backend(reply("ok"), model="hermes-agent")
        messages = [{"role": "system", "content": "be nice"},
                    {"role": "user", "content": "hi"}]

        llm.ask(messages)

        path, kwargs = client.post.call_args[0][0], client.post.call_args[1]
        assert path == "/chat/completions"
        assert kwargs["json"]["messages"] == messages
        assert kwargs["json"]["model"] == "hermes-agent"

    def test_think_is_accepted_and_ignored(self):
        """The agent picks its own reasoning depth; there's no OpenAI field for
        it, but the LLMBackend protocol still passes the argument."""
        llm, client = backend(reply("ok"))

        llm.ask([], think=False)

        assert "think" not in client.post.call_args[1]["json"]

    def test_reasoning_block_is_stripped(self):
        llm, _ = backend(reply("<think>hmm</think>final answer"))
        assert llm.ask([]) == "final answer"

    def test_none_content_is_not_a_crash(self):
        """A turn that produced only tool calls can come back with null
        content; that should be an empty reply, not a TypeError."""
        llm, _ = backend(reply(None))
        assert llm.ask([]) == ""

    def test_http_error_propagates(self):
        llm, client = backend()
        client.post.return_value = Mock(
            status_code=500, raise_for_status=Mock(side_effect=RuntimeError("500 server error")))

        with pytest.raises(RuntimeError, match="500 server error"):
            llm.ask([])

    def test_unexpected_shape_raises_a_clear_error(self):
        llm, client = backend()
        bad = Mock(status_code=200)
        bad.json.return_value = {"unexpected": "shape"}
        client.post.return_value = bad

        with pytest.raises(HermesAgentUnavailable, match="unexpected response shape"):
            llm.ask([])


class TestCheck:
    def _models(self, ids, status=200):
        response = Mock(status_code=status)
        response.json.return_value = {"data": [{"id": i} for i in ids]}
        return response

    def test_missing_model_warns_but_does_not_raise(self):
        llm, client = backend(model="hermes-agent")
        client.get.return_value = self._models(["something-else"])

        assert "not offered" in llm.check()

    def test_present_model_returns_no_warning(self):
        llm, client = backend(model="hermes-agent")
        client.get.return_value = self._models(["hermes-agent", "other"])

        assert llm.check() is None

    def test_an_empty_model_list_is_not_treated_as_missing(self):
        """Some OpenAI-compatible servers return no catalogue; that isn't a
        reason to warn about the model the user explicitly asked for."""
        llm, client = backend()
        client.get.return_value = self._models([])

        assert llm.check() is None

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_key_names_the_env_var_to_set(self, status):
        llm, client = backend()
        client.get.return_value = Mock(status_code=status)

        with pytest.raises(HermesAgentUnavailable, match="HERMES_API_KEY"):
            llm.check()

    def test_unreachable_agent_suggests_the_fix(self):
        import httpx

        llm, client = backend()
        client.get.side_effect = httpx.ConnectError("connection refused")

        with pytest.raises(HermesAgentUnavailable, match="hermes gateway"):
            llm.check()


class TestConfiguration:
    def test_defaults_point_at_a_local_gateway(self):
        llm, _ = backend()
        assert llm.url == DEFAULT_URL.rstrip("/")
        assert llm.model == DEFAULT_MODEL

    def test_trailing_slash_in_the_url_does_not_double_up(self):
        llm, _ = backend(url="http://host:8642/v1/")
        assert llm.url == "http://host:8642/v1"

    def test_timeout_default_allows_for_tool_calls(self):
        """A chat-length timeout would abort a real agent turn midway."""
        assert DEFAULT_TIMEOUT >= 120

    def test_key_falls_back_to_the_environment(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_KEY", "from-env")
        llm, _ = backend()
        assert llm.key == "from-env"

    def test_an_explicit_key_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_KEY", "from-env")
        llm, _ = backend(key="explicit")
        assert llm.key == "explicit"

    def test_from_args_reads_every_flag(self):
        import argparse

        args = argparse.Namespace(hermes_url="http://box:9000/v1", hermes_key="k",
                                  hermes_model="m", hermes_timeout=42.0)
        llm = HermesAgentLLM.from_args(args)

        assert (llm.url, llm.key, llm.model, llm.timeout) == ("http://box:9000/v1", "k", "m", 42.0)

    def test_it_is_registered_and_gets_its_flags_into_a_parser(self):
        import argparse

        from voicepipe.registry import LLM

        assert "hermes-agent" in LLM.names()
        ap = argparse.ArgumentParser()
        LLM.add_arguments(ap)
        assert ap.parse_args([]).hermes_url == DEFAULT_URL


# Streaming (ask_stream + the SSE parser) is covered in tests/test_streaming.py.
