"""voicepipe.backends.ollama — pure logic (strip_think) and ask()/check()
against a mocked Ollama client (no network, no real model)."""
from unittest.mock import Mock, patch

import pytest

from voicepipe.backends.ollama import OllamaLLM, OllamaUnavailable
from voicepipe.text import strip_think


class TestStripThink:
    def test_no_think_tag_passes_through(self):
        assert strip_think("just a reply") == "just a reply"

    def test_strips_think_block(self):
        assert strip_think("<think>reasoning...</think>the actual reply") == "the actual reply"

    def test_strips_surrounding_whitespace(self):
        assert strip_think("  <think>x</think>  padded  ") == "padded"

    def test_only_a_closing_tag_still_splits(self):
        # strip_think only looks for the closing tag, matching a model that
        # never emitted an opening <think> (e.g. thinking disabled server-side)
        assert strip_think("no opener</think>reply") == "reply"


def backend(chat, model="rina"):
    """An OllamaLLM whose client is a mock — no `ollama` package, no server."""
    return OllamaLLM(model=model, client=Mock(chat=chat))


class TestAsk:
    def test_think_none_sends_no_think_kwarg(self):
        chat = Mock(return_value={"message": {"content": "hi"}})

        result = backend(chat).ask([{"role": "user", "content": "hey"}], think=None)

        assert result == "hi"
        chat.assert_called_once_with(model="rina", messages=[{"role": "user", "content": "hey"}])

    def test_default_think_is_none_so_no_kwarg_is_sent(self):
        chat = Mock(return_value={"message": {"content": "hi"}})

        backend(chat).ask([])

        chat.assert_called_once_with(model="rina", messages=[])

    def test_think_false_sends_think_kwarg(self):
        chat = Mock(return_value={"message": {"content": "hi"}})

        backend(chat).ask([], think=False)

        chat.assert_called_once_with(model="rina", messages=[], think=False)

    def test_falls_back_when_model_rejects_think_kwarg(self):
        # the first call (with think=) raises because the model doesn't support
        # it; ask() should retry once without the kwarg rather than propagate
        calls = []

        def chat(*, model, messages, think=None):
            calls.append(think)
            if think is not None:
                raise ValueError("unknown parameter: think")
            return {"message": {"content": "ok"}}

        assert backend(chat, model="some-model").ask([], think=False) == "ok"
        assert calls == [False, None]  # first attempt with think=, then the bare retry

    def test_unrelated_error_propagates(self):
        chat = Mock(side_effect=RuntimeError("ollama is down"))

        with pytest.raises(RuntimeError, match="ollama is down"):
            backend(chat).ask([], think=False)

    def test_reply_with_think_block_is_stripped(self):
        chat = Mock(return_value={"message": {"content": "<think>hmm</think>final answer"}})

        assert backend(chat).ask([], think=None) == "final answer"


class TestCheck:
    """check() reports problems by raising, never by exiting the process, so
    an embedding caller decides what a failure means."""

    def _backend(self, models=("rina",), model="rina"):
        client = Mock()
        client.list.return_value = Mock(models=[Mock(model=m) for m in models])
        return OllamaLLM(host="http://localhost:11434", model=model, client=client)

    def test_unreachable_server_raises(self):
        with patch("socket.create_connection", side_effect=OSError("connection refused")):
            with pytest.raises(OllamaUnavailable, match="can't reach"):
                self._backend().check()

    def test_tags_failure_raises(self):
        llm = self._backend()
        llm.client.list.side_effect = RuntimeError("500 server error")

        with patch("socket.create_connection"):
            with pytest.raises(OllamaUnavailable, match="/api/tags failed"):
                llm.check()

    def test_reachable_with_the_model_present_returns_no_warning(self):
        with patch("socket.create_connection"):
            assert self._backend(models=("rina", "qwen3")).check() is None

    def test_missing_model_warns_but_does_not_raise(self):
        with patch("socket.create_connection"):
            warning = self._backend(models=("qwen3",), model="rina").check()

        assert "not found" in warning and "qwen3" in warning

    def test_latest_tag_counts_as_present(self):
        with patch("socket.create_connection"):
            assert self._backend(models=("rina:latest",), model="rina").check() is None

    def test_socket_uses_the_hosts_port(self):
        llm = OllamaLLM(host="http://media:9999", model="rina", client=Mock())
        llm.client.list.return_value = Mock(models=[Mock(model="rina")])

        with patch("socket.create_connection") as conn:
            llm.check()

        assert conn.call_args[0][0] == ("media", 9999)

    def test_default_port_when_the_host_omits_one(self):
        llm = OllamaLLM(host="http://media", model="rina", client=Mock())
        llm.client.list.return_value = Mock(models=[Mock(model="rina")])

        with patch("socket.create_connection") as conn:
            llm.check()

        assert conn.call_args[0][0] == ("media", 11434)


class TestFromArgs:
    def test_builds_from_the_shared_host_and_model_flags(self):
        args = Mock(host="http://media:11434", model="qwen3")

        with patch.object(OllamaLLM, "_make_client", return_value=Mock()) as make:
            llm = OllamaLLM.from_args(args)

        assert (llm.host, llm.model) == ("http://media:11434", "qwen3")
        make.assert_called_once()
