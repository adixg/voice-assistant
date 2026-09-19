from unittest.mock import Mock

import pytest

from voicepipe.backends.openai_compatible import OpenAICompatibleLLM, OpenAICompatibleUnavailable
from voicepipe.registry import LLM, stream_reply


def _response(payload):
    result = Mock()
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    return result


def test_chat_completion_and_reasoning_stripping():
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {"content": "<think>x</think>ok"}}]})
    llm = OpenAICompatibleLLM(url="http://llama:8080/v1", model="qwen", client=client)
    assert llm.ask([{"role": "user", "content": "hi"}]) == "ok"
    assert client.post.call_args.args == ("/chat/completions",)
    assert client.post.call_args.kwargs["json"]["model"] == "qwen"


def test_invalid_completion_is_a_backend_error():
    client = Mock()
    client.post.return_value = _response({})
    with pytest.raises(OpenAICompatibleUnavailable):
        OpenAICompatibleLLM(client=client).ask([])


def test_streaming_openai_sse():
    streamed = Mock()
    streamed.raise_for_status.return_value = None
    streamed.iter_lines.return_value = [
        'data: {"choices":[{"delta":{"content":"hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}', "data: [DONE]",
    ]
    context = Mock()
    context.__enter__ = Mock(return_value=streamed)
    context.__exit__ = Mock(return_value=False)
    client = Mock()
    client.stream.return_value = context
    assert list(stream_reply(OpenAICompatibleLLM(client=client), [])) == [
        ("delta", "hel"), ("delta", "lo"), ("final", "hello")]


def test_registered():
    assert "openai-compatible" in LLM.names()
