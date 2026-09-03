"""voicepipe.llm — pure logic (strip_think) and ask() against a mocked
Ollama client (no network, no real model)."""
from unittest.mock import Mock

import pytest

from voicepipe.llm import ask, strip_think


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


class TestAsk:
    def _client(self, chat_mock):
        c = Mock()
        c.chat = chat_mock
        return c

    def test_think_none_sends_no_think_kwarg(self):
        chat = Mock(return_value={"message": {"content": "hi"}})
        client = self._client(chat)
        result = ask(client, "rina", [{"role": "user", "content": "hey"}], think=None)
        assert result == "hi"
        chat.assert_called_once_with(model="rina", messages=[{"role": "user", "content": "hey"}])

    def test_think_false_sends_think_kwarg(self):
        chat = Mock(return_value={"message": {"content": "hi"}})
        client = self._client(chat)
        ask(client, "rina", [], think=False)
        chat.assert_called_once_with(model="rina", messages=[], think=False)

    def test_falls_back_when_model_rejects_think_kwarg(self):
        # first call (with think=) raises because the model doesn't support it;
        # ask() should retry once without the kwarg rather than propagating
        calls = []

        def chat(*, model, messages, think=None):
            calls.append(think)
            if think is not None:
                raise ValueError("unknown parameter: think")
            return {"message": {"content": "ok"}}

        client = self._client(chat)
        result = ask(client, "some-model", [], think=False)
        assert result == "ok"
        assert calls == [False, None]  # first attempt with think=, then the bare retry

    def test_unrelated_error_propagates(self):
        chat = Mock(side_effect=RuntimeError("ollama is down"))
        client = self._client(chat)
        with pytest.raises(RuntimeError, match="ollama is down"):
            ask(client, "rina", [], think=False)

    def test_reply_with_think_block_is_stripped(self):
        chat = Mock(return_value={"message": {"content": "<think>hmm</think>final answer"}})
        client = self._client(chat)
        assert ask(client, "rina", [], think=None) == "final answer"
