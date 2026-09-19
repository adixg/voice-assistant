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


def test_mcp_tool_loop_executes_call_and_returns_followup():
    client = Mock()
    client.post.side_effect = [
        _response({"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call-1", "function": {"name": "get_gpu_status", "arguments": "{}"}
        }]}}]}),
        _response({"choices": [{"message": {"content": "GPU is healthy."}}]}),
    ]

    class FakeMCP:
        def openai_tools(self):
            return [{"type": "function", "function": {
                "name": "get_gpu_status", "description": "Read GPU status",
                "parameters": {"type": "object", "properties": {}},
            }}]

        def call(self, name, arguments):
            assert name == "get_gpu_status"
            assert arguments == {}
            return '{"utilization": []}'

    llm = OpenAICompatibleLLM(url="http://llama:8080/v1", model="qwen", client=client,
                              mcp_client=FakeMCP())
    assert llm.ask([{"role": "user", "content": "check the GPU"}]) == "GPU is healthy."
    first = client.post.call_args_list[0].kwargs["json"]
    second = client.post.call_args_list[1].kwargs["json"]
    assert first["tools"][0]["function"]["name"] == "get_gpu_status"
    assert second["messages"][-1] == {
        "role": "tool", "tool_call_id": "call-1", "content": '{"utilization": []}'
    }


def test_mcp_tool_loop_executes_search_web_with_query_and_returns_sources():
    client = Mock()
    client.post.side_effect = [
        _response({"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "search-1", "function": {
                "name": "search_web",
                "arguments": '{"query":"who is the president of the USA","max_results":3}',
            }
        }]}}]}),
        _response({"choices": [{"message": {
            "content": "The search result says the answer is available here: https://example.test/source"
        }}]}),
    ]

    class FakeMCP:
        def openai_tools(self):
            return [{"type": "function", "function": {
                "name": "search_web", "description": "Search the web",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"}, "max_results": {"type": "integer"}},
                    "required": ["query"]},
            }}]

        def call(self, name, arguments):
            assert name == "search_web"
            assert arguments == {"query": "who is the president of the USA", "max_results": 3}
            return '{"query":"who is the president of the USA","results":[{"url":"https://example.test/source"}]}'

    llm = OpenAICompatibleLLM(url="http://llama:8080/v1", model="qwen", client=client,
                              mcp_client=FakeMCP())
    reply = llm.ask([{"role": "user", "content": "Who is the president of the USA?"}])
    assert "https://example.test/source" in reply
    tool_message = client.post.call_args_list[1].kwargs["json"]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "search-1"
    assert "president of the USA" in tool_message["content"]


def test_mcp_tool_loop_rejects_malformed_arguments():
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "call-1", "function": {
            "name": "get_gpu_status", "arguments": "not-json",
        }}],
    }}]})

    class FakeMCP:
        def openai_tools(self):
            return []

    with pytest.raises(OpenAICompatibleUnavailable, match="chat completion failed"):
        OpenAICompatibleLLM(client=client, mcp_client=FakeMCP()).ask([])


def test_mcp_tool_loop_stops_after_max_rounds():
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "call-1", "function": {
            "name": "get_gpu_status", "arguments": "{}",
        }}],
    }}]})

    class FakeMCP:
        def openai_tools(self):
            return [{"type": "function", "function": {"name": "get_gpu_status"}}]

        def call(self, _name, _arguments):
            return "{}"

    with pytest.raises(OpenAICompatibleUnavailable, match="exceeded max_tool_rounds"):
        OpenAICompatibleLLM(client=client, mcp_client=FakeMCP(), max_tool_rounds=1).ask([])


def test_mcp_tool_error_is_normalized():
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "call-1", "function": {
            "name": "get_gpu_status", "arguments": "{}",
        }}],
    }}]})

    class FakeMCP:
        def openai_tools(self):
            return [{"type": "function", "function": {"name": "get_gpu_status"}}]

        def call(self, _name, _arguments):
            raise RuntimeError("tool failed")

    with pytest.raises(OpenAICompatibleUnavailable, match="chat completion failed"):
        OpenAICompatibleLLM(client=client, mcp_client=FakeMCP()).ask([])
