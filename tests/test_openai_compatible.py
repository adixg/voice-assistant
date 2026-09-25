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


def _sent(client):
    return client.post.call_args.kwargs["json"]


@pytest.mark.parametrize("think,expected", [(False, {"enable_thinking": False}),
                                            (True, {"enable_thinking": True})])
def test_think_flag_reaches_the_server(think, expected):
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {"content": "ok"}}]})
    OpenAICompatibleLLM(client=client).ask([], think=think)
    assert _sent(client)["chat_template_kwargs"] == expected


def test_think_none_leaves_the_server_default():
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {"content": "ok"}}]})
    OpenAICompatibleLLM(client=client).ask([], think=None)
    assert "chat_template_kwargs" not in _sent(client)


def test_thinking_switch_can_be_disabled_for_strict_endpoints():
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {"content": "ok"}}]})
    OpenAICompatibleLLM(client=client, thinking_switch=False).ask([], think=False)
    assert "chat_template_kwargs" not in _sent(client)


def test_streaming_sends_think_flag():
    streamed = Mock()
    streamed.raise_for_status.return_value = None
    streamed.iter_lines.return_value = ["data: [DONE]"]
    context = Mock()
    context.__enter__ = Mock(return_value=streamed)
    context.__exit__ = Mock(return_value=False)
    client = Mock()
    client.stream.return_value = context
    list(OpenAICompatibleLLM(client=client).ask_stream([], think=False))
    body = client.stream.call_args.kwargs["json"]
    assert body["stream"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_tool_loop_sends_think_flag_on_every_round():
    client = Mock()
    client.post.side_effect = [
        _response({"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "c1", "function": {"name": "get_gpu_status", "arguments": "{}"}}]}}]}),
        _response({"choices": [{"message": {"content": "done"}}]}),
    ]
    mcp = Mock()
    mcp.openai_tools.return_value = [{"type": "function", "function": {"name": "get_gpu_status"}}]
    mcp.call.return_value = "ok"
    OpenAICompatibleLLM(client=client, mcp_client=mcp).ask([], think=False)
    for call in client.post.call_args_list:
        assert call.kwargs["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_env_can_disable_the_switch(monkeypatch):
    import argparse
    monkeypatch.setenv("LLM_THINKING_SWITCH", "0")
    ap = argparse.ArgumentParser()
    OpenAICompatibleLLM.add_arguments(ap)
    args = ap.parse_args([])
    assert OpenAICompatibleLLM.from_args(args).thinking_switch is False


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


def test_tool_loop_streams_a_status_before_each_tool_call():
    """The Stick shows it ("status:" frame) instead of a frozen thinking screen."""
    client = Mock()
    client.post.side_effect = [
        _response({"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call-1", "function": {"name": "get_weather",
                                         "arguments": '{"location": "Atlanta, Georgia"}'}
        }]}}]}),
        _response({"choices": [{"message": {"content": "No umbrella needed."}}]}),
    ]
    calls = []

    class FakeMCP:
        def openai_tools(self):
            return []

        def call(self, name, arguments):
            calls.append(name)
            return "{}"

    llm = OpenAICompatibleLLM(url="http://llama:8080/v1", model="qwen", client=client,
                              mcp_client=FakeMCP())
    events = []
    for kind, text in stream_reply(llm, [{"role": "user", "content": "umbrella?"}]):
        events.append((kind, text, list(calls)))

    assert events[0] == ("status", "checking the weather in Atlanta", [])  # before the tool ran
    assert events[-1][:2] == ("final", "No umbrella needed.")
    assert [e[0] for e in events] == ["status", "tools", "final"]


def test_tool_turn_reports_its_calls_for_the_history():
    """Kept in the conversation so the model sees changes were tool calls."""
    import json as _json
    client = Mock()
    client.post.side_effect = [
        _response({"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "set_stick_brightness", "arguments": '{"percent": 5}'}}]}}]}),
        _response({"choices": [{"message": {"content": "Dimmed it to 5%."}}]}),
    ]
    mcp = Mock()
    mcp.openai_tools.return_value = [{"type": "function", "function": {"name": "set_stick_brightness"}}]
    mcp.call.return_value = '{"brightness_percent": 5}' + " " * 1000
    llm = OpenAICompatibleLLM(client=client, mcp_client=mcp)
    events = dict(stream_reply(llm, [{"role": "user", "content": "dim it to five"}]))
    kept = _json.loads(events["tools"])
    assert kept[0]["role"] == "assistant"
    assert kept[0]["tool_calls"][0]["function"] == {"name": "set_stick_brightness", "arguments": '{"percent": 5}'}
    assert kept[1]["role"] == "tool" and kept[1]["tool_call_id"] == "call-1"
    assert kept[1]["content"].startswith('{"brightness_percent": 5}') and kept[1]["content"].endswith("(trimmed)")
    assert events["final"] == "Dimmed it to 5%."


def test_no_tools_event_when_no_tool_ran():
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {"content": "hi"}}]})
    mcp = Mock()
    mcp.openai_tools.return_value = []
    events = list(stream_reply(OpenAICompatibleLLM(client=client, mcp_client=mcp), []))
    assert events == [("final", "hi")]


def test_tool_status_names_the_search_and_falls_back_for_unknown_tools():
    from voicepipe.backends.openai_compatible import tool_status
    assert tool_status("search_web", {"query": "falcons score"}) == "searching the web: falcons score"
    assert tool_status("get_time", {}) == "checking the time"
    assert tool_status("mystery_tool", {}) == "using mystery_tool"


def _gpu_mcp():
    mcp = Mock()
    mcp.openai_tools.return_value = [{"type": "function", "function": {"name": "get_gpu_status"}}]
    mcp.call.return_value = '{"sensors": [{"temperature_c": 48}]}'
    return mcp


def test_a_reply_that_promises_to_check_is_sent_back_to_actually_check():
    """"Let me check the GPU temperatures for you. One moment..." ended the
    turn with nothing checked (2026-09-25): the reply is the end of the turn."""
    import json as _json
    from voicepipe.backends.openai_compatible import PROMISE_NUDGE
    client = Mock()
    client.post.side_effect = [
        _response({"choices": [{"message": {"content": "Let me check the GPU temperatures. One moment..."}}]}),
        _response({"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {"name": "get_gpu_status", "arguments": "{}"}}]}}]}),
        _response({"choices": [{"message": {"content": "The 1650 is at 48 C."}}]}),
    ]
    events = dict(stream_reply(OpenAICompatibleLLM(client=client, mcp_client=_gpu_mcp()),
                               [{"role": "user", "content": "how hot are my GPUs?"}]))
    assert events["final"] == "The 1650 is at 48 C."
    # (the mock keeps a reference to the growing message list, hence [1:3])
    sent = client.post.call_args_list[1].kwargs["json"]["messages"]
    assert sent[1:3] == [{"role": "assistant", "content": "Let me check the GPU temperatures. One moment..."},
                         {"role": "user", "content": PROMISE_NUDGE}]
    # The history keeps the tool call, not the broken promise or the note.
    kept = _json.loads(events["tools"])
    assert [m["role"] for m in kept] == ["assistant", "tool"] and kept[0]["tool_calls"]


def test_a_second_promise_is_let_through_rather_than_looping():
    client = Mock()
    client.post.side_effect = [
        _response({"choices": [{"message": {"content": "Hold on, let me look."}}]}),
        _response({"choices": [{"message": {"content": "I'll check on that."}}]}),
    ]
    llm = OpenAICompatibleLLM(client=client, mcp_client=_gpu_mcp())
    assert llm.ask([{"role": "user", "content": "x"}]) == "I'll check on that."
    assert client.post.call_count == 2


@pytest.mark.parametrize("reply", ["Let me know if you need anything else!",
                                   "I checked, it's fine.", "Sure, see you tomorrow."])
def test_ordinary_replies_are_not_mistaken_for_promises(reply):
    client = Mock()
    client.post.return_value = _response({"choices": [{"message": {"content": reply}}]})
    llm = OpenAICompatibleLLM(client=client, mcp_client=_gpu_mcp())
    assert llm.ask([{"role": "user", "content": "x"}]) == reply
    assert client.post.call_count == 1
