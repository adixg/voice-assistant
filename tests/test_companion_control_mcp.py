"""Tests for the dependency-free companion-control MCP transport and tools."""
import json

import pytest

from urllib.parse import parse_qs, urlparse

from tools import companion_control_mcp as mcp


def prom_result(items):
    return {"status": "success", "data": {"resultType": "vector", "result": items}}


def sample(labels, value):
    return {"metric": labels, "value": [0, str(value)]}


def test_initialize_negotiates_the_client_protocol_version():
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-03-26"}})
    assert response["result"]["protocolVersion"] == "2025-03-26"
    assert response["result"]["capabilities"] == {"tools": {"listChanged": False}}


def test_tools_list_is_read_only_except_the_sticks_settings_notes_reminders_and_claude():
    """The permission boundary: adding any other write tool must change this test."""
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [tool["name"] for tool in response["result"]["tools"]] == [
        "get_service_health", "get_gpu_status", "get_agent_status", "get_model_status",
        "get_time", "search_web", "get_weather",
        "get_stick_settings", "set_stick_volume", "set_stick_brightness",
        "read_notes", "add_note", "write_notes",
        "set_reminder", "list_reminders", "cancel_reminder", "ask_claude", "get_claude_usage",
        "list_voices", "set_voice"]


def test_current_time_returns_requested_timezone(monkeypatch):
    result = mcp.current_time({"timezone": "UTC"})
    assert result["timezone"] == "UTC"
    assert result["iso"].endswith("+00:00")
    assert result["date"] == result["iso"][:10]
    assert result["day_of_week"]


def test_current_time_rejects_unknown_timezone():
    with pytest.raises(mcp.ControlPlaneError, match="unknown IANA timezone"):
        mcp.current_time({"timezone": "Moon/Base"})


def test_model_status_reports_configured_route_and_served_model(monkeypatch):
    monkeypatch.setenv("LLM_HOST", "http://llama-cpp-rtx4060:8080/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3-8b")
    result = mcp.model_status()
    assert result == {
        "configured": True,
        "route": "rtx4060",
        "endpoint": "http://llama-cpp-rtx4060:8080/v1",
        "requested_model": "qwen3-8b",
        "served_models": ["qwen3-8b"],
        "active_model_matches": True,
        "source": "agent scheduler configuration",
        "backend": "openai-compatible",
    }


def test_model_status_reports_gtx1650_route(monkeypatch):
    monkeypatch.setenv("LLM_HOST", "http://llama-cpp-gtx1650:8080/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3.5-4b")
    result = mcp.model_status()
    assert result["route"] == "gtx1650"
    assert result["active_model_matches"] is True


def test_search_web_returns_compact_sources(monkeypatch):
    monkeypatch.setenv("COMPANION_CONTROL_SEARXNG_URL", "http://searxng.test")

    def get_json(url):
        parsed = parse_qs(urlparse(url).query)
        assert parsed["q"] == ["python mcp"]
        assert parsed["format"] == ["json"]
        return {"results": [
            {"title": "MCP", "url": "https://example.test/mcp", "content": "A protocol",
             "publishedDate": "2026-01-01", "engines": ["example"]},
        ]}

    result = mcp.search_web({"query": "python mcp", "max_results": 3}, get_json)
    assert result["results"] == [{
        "title": "MCP", "url": "https://example.test/mcp", "snippet": "A protocol",
        "published": "2026-01-01", "engines": ["example"],
    }]


def test_search_web_validates_query_and_freshness():
    with pytest.raises(mcp.ControlPlaneError, match="non-empty"):
        mcp.search_web({}, lambda _url: {})
    with pytest.raises(mcp.ControlPlaneError, match="freshness"):
        mcp.search_web({"query": "test", "freshness": "forever"}, lambda _url: {})


def test_search_web_is_callable_through_mcp():
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                   "params": {"name": "search_web", "arguments": {"query": "test"}}})
    # The default in-cluster endpoint is unavailable in unit tests, but the
    # protocol should turn that dependency failure into an MCP tool error.
    assert response["result"]["isError"] is True


def test_weather_reports_rain_window(monkeypatch):
    calls = []

    def get_json(url):
        calls.append(url)
        if "geocoding-api" in url:
            return {"results": [{"name": "Atlanta", "country": "United States",
                                  "latitude": 33.75, "longitude": -84.39}]}
        return {
            "timezone": "America/New_York",
            "current": {"time": "2026-09-19T12:00", "temperature_2m": 78,
                         "precipitation": 0, "rain": 0, "showers": 0, "weather_code": 1},
            "hourly": {
                "time": ["2026-09-19T12:00", "2026-09-19T13:00", "2026-09-19T14:00",
                         "2026-09-19T15:00", "2026-09-19T16:00", "2026-09-19T17:00"],
                "precipitation_probability": [10, 80, 70, 60, 10, 5],
                "precipitation": [0, 0.4, 0.3, 0.2, 0, 0],
                "rain": [0, 0.4, 0.3, 0.2, 0, 0],
                "showers": [0, 0, 0, 0, 0, 0],
                "weather_code": [1, 61, 61, 61, 1, 1],
            },
            "daily": {
                "time": ["2026-09-19", "2026-09-20"],
                "precipitation_probability_max": [80, 10],
                "precipitation_sum": [0.9, 0],
                "rain_sum": [0.9, 0],
                "showers_sum": [0, 0],
                "weather_code": [61, 1],
            },
        }

    result = mcp.weather({"location": "Atlanta, GA"}, get_json)
    assert len(calls) == 2
    assert result["location"] == "Atlanta"
    assert result["rain"] == {
        "raining_now": False,
        "expected": True,
        "starts": "2026-09-19T13:00",
        "stops": "2026-09-19T17:00",
        "today_expected": True,
        "today_starts": "2026-09-19T13:00",
        "today_stops": "2026-09-19T17:00",
        "stop_note": "Estimated from hourly forecast; conditions can change.",
    }
    assert result["daily_forecast"][0]["rain_expected"] is True
    assert result["daily_forecast"][1]["rain_expected"] is False


def test_weather_uses_fixed_georgia_tech_coordinates():
    urls = []

    def get_json(url):
        urls.append(url)
        return {"timezone": "America/New_York",
                "current": {"time": "2026-09-19T12:00", "weather_code": 1,
                             "precipitation": 0, "rain": 0, "showers": 0},
                "hourly": {"time": [], "precipitation_probability": [],
                           "precipitation": [], "rain": [], "showers": [], "weather_code": []}}

    result = mcp.weather({"location": "Georgia Tech, Atlanta"}, get_json)
    assert urls[0].startswith("https://api.open-meteo.com/v1/forecast?")
    assert "geocoding-api" not in urls[0]
    assert result["coordinates"] == {"latitude": 33.7759, "longitude": -84.3975}


def test_weather_rejects_unknown_location():
    with pytest.raises(mcp.ControlPlaneError, match="no weather location"):
        mcp.weather({"location": "Atlantis"}, lambda _url: {"results": []})


def test_service_health_normalizes_prometheus_results(monkeypatch):
    def get_json(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        if query.startswith("up{"):
            return prom_result([sample({"service": "agent", "instance": "10.42.0.1:8002"}, 1)])
        if query.startswith("max by"):
            return prom_result([sample({"pod": "agent-abc"}, 1)])
        return prom_result([sample({"pod": "tts-def", "container": "tts"}, 2)])

    monkeypatch.setenv("COMPANION_CONTROL_PROMETHEUS_URL", "http://prometheus.test")
    result = mcp.service_health(get_json)

    assert result["targets"] == [{"service": "agent", "instance": "10.42.0.1:8002", "up": True}]
    assert result["pods"] == [{"pod": "agent-abc", "ready": True}]
    assert result["restarts_last_hour"][0]["restarts_last_hour"] == 2.0


def test_gpu_status_reports_no_data_without_treating_it_as_a_failure(monkeypatch):
    monkeypatch.setenv("COMPANION_CONTROL_PROMETHEUS_URL", "http://prometheus.test")
    result = mcp.gpu_status(lambda _url: prom_result([]))
    assert result["utilization"] == []
    assert result["message"] == "No DCGM GPU samples are available yet."


def test_gpu_status_uses_exported_framebuffer_components(monkeypatch):
    monkeypatch.setenv("COMPANION_CONTROL_PROMETHEUS_URL", "http://prometheus.test")
    queries = []

    def get_json(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        queries.append(query)
        return prom_result([sample({"hostname": "arch-ssd", "gpu": "0"}, 42)])

    result = mcp.gpu_status(get_json)

    assert result["vram_utilization"][0]["vram_percent"] == 42.0
    assert "DCGM_FI_DEV_FB_TOTAL" not in queries[1]
    for metric in ("DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_FB_FREE", "DCGM_FI_DEV_FB_RESERVED"):
        assert metric in queries[1]


def test_gpu_status_normalizes_labels_and_vram(monkeypatch):
    monkeypatch.setenv("COMPANION_CONTROL_PROMETHEUS_URL", "http://prometheus.test")

    def get_json(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        if query == "DCGM_FI_DEV_GPU_UTIL":
            return prom_result([sample({"hostname": "arch-ssd", "gpu": "0"}, 73)])
        return prom_result([sample({"instance": "10.42.1.92:9400", "gpu": "0"}, 88)])

    result = mcp.gpu_status(get_json)
    assert result["utilization"] == [{"host": "arch-ssd", "gpu": "0", "utilization_percent": 73.0}]
    assert result["vram_utilization"] == [{"host": "10.42.1.92:9400", "gpu": "0", "vram_percent": 88.0}]


def test_gpu_status_reports_temperature_power_and_clock_per_gpu(monkeypatch):
    monkeypatch.setenv("COMPANION_CONTROL_PROMETHEUS_URL", "http://prometheus.test")
    labels = {"hostname": "arch-ssd", "gpu": "0", "modelName": "NVIDIA GeForce GTX 1650"}
    values = {"DCGM_FI_DEV_GPU_TEMP": 48, "DCGM_FI_DEV_POWER_USAGE": 12.345,
              'aicompanion_gpu_clock_mhz{clock="sm"}': 300}

    def get_json(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        return prom_result([sample(labels, values[query])] if query in values else [])

    assert mcp.gpu_status(get_json)["sensors"] == [
        {"host": "arch-ssd", "gpu": "0", "name": "NVIDIA GeForce GTX 1650",
         "temperature_c": 48.0, "power_w": 12.3, "sm_clock_mhz": 300.0}]


def test_service_health_wraps_prometheus_failure():
    def fail(_url):
        raise mcp.ControlPlaneError("Prometheus unavailable")

    with pytest.raises(mcp.ControlPlaneError, match="unavailable"):
        mcp.service_health(fail)


def test_tool_call_rejects_arguments_for_a_zero_argument_tool():
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "get_gpu_status", "arguments": {"unsafe": True}}})
    assert response["result"]["isError"] is True
    assert "takes no arguments" in response["result"]["content"][0]["text"]


def test_notifications_do_not_receive_a_json_rpc_response():
    assert mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_gpu_status_reports_active_throttling_but_not_idle(monkeypatch):
    monkeypatch.setenv("COMPANION_CONTROL_PROMETHEUS_URL", "http://prometheus.test")
    queries = []

    def get_json(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        queries.append(query)
        if "aicompanion_gpu_throttle" in query:
            return prom_result([sample({"hostname": "arch-ssd", "gpu": "0", "reason": "hw_thermal_slowdown"}, 1)])
        return prom_result([sample({"hostname": "arch-ssd", "gpu": "0"}, 42)])

    result = mcp.gpu_status(get_json)

    assert result["throttling"] == [{"host": "arch-ssd", "gpu": "0", "reason": "hw_thermal_slowdown"}]
    assert 'reason!="gpu_idle"' in queries[-1]
    assert result["utilization"][0]["host"] == "arch-ssd"  # the real label is lowercase "hostname"


class FakeGateway:
    """Stands in for gateway_request: the Stick applies whatever is POSTed."""

    def __init__(self, volume=255, brightness=38):
        self.state = {"volume": volume, "brightness": brightness, "firmware": "a36b55d"}
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            self.state.update(body)
        return dict(self.state)


def test_stick_settings_reports_percent_and_screen_off():
    result = mcp.stick_settings(request=FakeGateway(volume=128, brightness=0))
    assert result == {"volume_percent": 50, "brightness_percent": 0, "screen_off": True,
                      "firmware": "a36b55d"}


def test_loud_volume_carries_the_brownout_note():
    assert "reboot" in mcp.stick_settings(request=FakeGateway(volume=255))["note"]


def test_set_volume_absolute_posts_only_that_field():
    gateway = FakeGateway()
    result = mcp.set_stick_volume({"percent": 40}, request=gateway)
    assert gateway.calls[-1] == ("POST", "/device/settings", {"volume": 102})
    assert result["volume_percent"] == 40 and result["previous_percent"] == 100
    assert result["changed"] == "volume"


def test_set_brightness_relative_is_clamped_and_zero_means_screen_off():
    gateway = FakeGateway(brightness=26)  # 10%
    result = mcp.set_stick_brightness({"change": -30}, request=gateway)
    assert gateway.calls[-1][2] == {"brightness": 0}
    assert result["screen_off"] is True
    result = mcp.set_stick_brightness({"change": 500}, request=gateway)
    assert result["brightness_percent"] == 100


@pytest.mark.parametrize("args", [{}, {"percent": 10, "change": 5}, {"percent": "10"}, {"change": True}])
def test_set_needs_exactly_one_whole_number(args):
    with pytest.raises(mcp.ControlPlaneError):
        mcp.set_stick_volume(args, request=FakeGateway())


def test_gateway_errors_surface_as_tool_errors(monkeypatch):
    def down(method, path, body=None):
        raise mcp.ControlPlaneError("no Stick connected")

    monkeypatch.setitem(mcp.TOOL_HANDLERS, "set_stick_volume",
                        lambda args: mcp.set_stick_volume(args, request=down))
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "set_stick_volume", "arguments": {"percent": 5}}})
    assert response["result"]["isError"] is True
    assert "no Stick connected" in response["result"]["content"][0]["text"]


@pytest.fixture
def notes_file(tmp_path, monkeypatch):
    path = tmp_path / "rina" / "notes.md"
    path.parent.mkdir()
    monkeypatch.setenv("COMPANION_CONTROL_NOTES_FILE", str(path))
    return path


def call_tool(name, arguments=None):
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments or {}}})
    result = response["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def test_read_notes_on_a_missing_file_is_empty(notes_file):
    is_error, payload = call_tool("read_notes")
    assert not is_error
    assert payload["text"] == "" and payload["lines"] == 0


def test_add_note_appends_a_line_and_keeps_the_rest(notes_file):
    notes_file.write_text("# Notes\nbuy milk")  # no trailing newline, as an editor might leave it
    is_error, payload = call_tool("add_note", {"text": "  call mum on Sunday \n"})
    assert not is_error and payload["added"] == "call mum on Sunday"
    assert notes_file.read_text() == "# Notes\nbuy milk\ncall mum on Sunday\n"
    assert call_tool("read_notes")[1]["lines"] == 3


def test_write_notes_replaces_the_file_and_leaves_no_temp_files(notes_file):
    notes_file.write_text("old\n")
    is_error, payload = call_tool("write_notes", {"text": "new"})
    assert not is_error and payload["previous"]["lines"] == 1
    assert notes_file.read_text() == "new\n"
    # The replaced version is kept once, and no temp files are left behind.
    assert sorted(p.name for p in notes_file.parent.iterdir()) == ["notes.md", "notes.md.bak"]
    assert (notes_file.parent / "notes.md.bak").read_text() == "old\n"


def test_notes_writes_are_capped_and_leave_the_file_alone(notes_file):
    notes_file.write_text("keep\n")
    is_error, payload = call_tool("write_notes", {"text": "x" * (mcp.NOTES_MAX_BYTES + 1)})
    assert is_error and "20 KB" in payload["error"]
    assert call_tool("add_note", {"text": "   "})[0]
    assert notes_file.read_text() == "keep\n"


def test_notes_tools_reject_bad_arguments(notes_file):
    assert call_tool("add_note", {"text": 5})[0]
    assert call_tool("read_notes", {"path": "/etc/passwd"})[0]


def test_reminder_tools_go_through_the_gateway_and_summarize():
    calls = []

    def gateway(method, path, body=None):
        calls.append((method, path, body))
        reminder = {"id": "a1b2c3", "text": "stretch", "when": "3:00 PM today", "repeat": "none",
                    "due": "2026-09-25T15:00:00-04:00", "created": "x"}
        return {"now": "2:00 PM today", "reminders": [reminder]} if method == "GET" else reminder

    assert mcp.set_reminder({"text": "stretch", "at": "15:00"}, request=gateway) == {
        "set": {"id": "a1b2c3", "text": "stretch", "when": "3:00 PM today", "repeat": "none"}}
    assert mcp.list_reminders(request=gateway)["now"] == "2:00 PM today"
    assert mcp.cancel_reminder({"id": "a1b2c3"}, request=gateway)["cancelled"]["id"] == "a1b2c3"
    assert calls == [("POST", "/reminders", {"text": "stretch", "at": "15:00"}),
                     ("GET", "/reminders", None), ("DELETE", "/reminders/a1b2c3", None)]
    with pytest.raises(mcp.ControlPlaneError):
        mcp.cancel_reminder({"id": "../device/settings"}, request=gateway)


def test_stick_settings_include_the_battery_when_reported():
    import time as _time

    def gateway(method, path, body=None):
        return {"volume": 128, "brightness": 38, "firmware": "fw",
                "battery": {"percent": 76, "charging": False, "volts": 3.987,
                            "reported_at": _time.time() - 120}}

    result = mcp.stick_settings(request=gateway)
    assert result["battery_percent"] == 76 and result["charging"] is False
    assert result["battery_volts"] == 3.987
    assert 1.9 <= result["battery_reported_minutes_ago"] <= 2.1


def test_ask_claude_sends_a_spoken_style_request_and_returns_the_answer(monkeypatch):
    monkeypatch.setattr(mcp, "_claude_calls", {})
    monkeypatch.setenv("COMPANION_CONTROL_CLAUDE_MODEL", "claude-test")
    sent = []

    def claude(body):
        sent.append(body)
        return {"model": "claude-test", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "White Nights is a short story by Dostoevsky."}],
                "usage": {"input_tokens": 90, "output_tokens": 12}}

    result = mcp.ask_claude({"question": " What is White Nights about? "}, request=claude)
    assert result["answer"] == "White Nights is a short story by Dostoevsky."
    assert result["tokens"] == {"in": 90, "out": 12} and result["truncated"] is False
    [body] = sent
    assert body["model"] == "claude-test" and body["max_tokens"] == 300
    assert body["messages"] == [{"role": "user", "content": "What is White Nights about?"}]
    assert "spoken aloud" in body["system"] and "60 words" in body["system"]
    assert mcp.ask_claude({"question": "x", "detail": "detailed"}, request=claude) and sent[1]["max_tokens"] == 600


def test_ask_claude_is_capped_per_day_and_validates(monkeypatch):
    monkeypatch.setattr(mcp, "_claude_calls", {})
    monkeypatch.setenv("COMPANION_CONTROL_CLAUDE_DAILY_LIMIT", "2")
    claude = lambda body: {"content": [{"type": "text", "text": "ok"}]}
    day = lambda: "2026-09-25"
    for _ in range(2):
        mcp.ask_claude({"question": "q"}, request=claude, today=day)
    with pytest.raises(mcp.ControlPlaneError, match="daily limit of 2"):
        mcp.ask_claude({"question": "q"}, request=claude, today=day)
    assert mcp.ask_claude({"question": "q"}, request=claude, today=lambda: "2026-09-26")["answer"] == "ok"
    for bad in ({}, {"question": "  "}, {"question": "q", "detail": "essay"}):
        with pytest.raises(mcp.ControlPlaneError):
            mcp.ask_claude(bad, request=claude, today=lambda: "2026-09-27")
    with pytest.raises(mcp.ControlPlaneError, match="no text"):
        mcp.ask_claude({"question": "q"}, request=lambda body: {"content": []}, today=lambda: "2026-09-27")


def test_ask_claude_without_a_key_is_a_tool_error_not_a_crash(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(mcp, "_claude_calls", {})
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                   "params": {"name": "ask_claude", "arguments": {"question": "hi"}}})
    assert response["result"]["isError"] is True
    assert "ANTHROPIC_API_KEY" in response["result"]["content"][0]["text"]


def _claude_reply(tokens_in=1000, tokens_out=100):
    return lambda body: {"model": "claude-test", "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}


def test_ask_claude_records_each_call_and_its_estimated_cost(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setenv("COMPANION_CONTROL_CLAUDE_USAGE_FILE", str(ledger))
    monkeypatch.setenv("COMPANION_CONTROL_CLAUDE_PRICE_INPUT", "3")
    monkeypatch.setenv("COMPANION_CONTROL_CLAUDE_PRICE_OUTPUT", "15")
    result = mcp.ask_claude({"question": "q"}, request=_claude_reply(), today=lambda: "2026-09-25")
    assert result["estimated_cost_usd"] == 0.0045  # 1000 * 3/M + 100 * 15/M
    mcp.ask_claude({"question": "q"}, request=_claude_reply(2000, 0), today=lambda: "2026-10-01")
    [first, second] = mcp.read_claude_usage()
    assert first["date"] == "2026-09-25" and first["input_tokens"] == 1000 and first["cost_usd"] == 0.0045
    assert "q" not in json.dumps(first)  # tokens and cost only, never the question

    usage = mcp.claude_usage(today=lambda: "2026-10-01")
    assert usage["today"] == {"calls": 1, "input_tokens": 2000, "output_tokens": 0, "estimated_cost_usd": 0.006}
    assert usage["all_time"]["calls"] == 2 and usage["all_time"]["estimated_cost_usd"] == 0.0105
    assert usage["this_month"]["calls"] == 1


def test_the_daily_cap_counts_from_the_ledger_so_a_restart_does_not_reset_it(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setenv("COMPANION_CONTROL_CLAUDE_USAGE_FILE", str(ledger))
    monkeypatch.setenv("COMPANION_CONTROL_CLAUDE_DAILY_LIMIT", "2")
    ledger.write_text("\n".join(json.dumps({"date": "2026-09-25", "input_tokens": 1}) for _ in range(2)) + "\n")
    monkeypatch.setattr(mcp, "_claude_calls", {})  # a fresh process
    with pytest.raises(mcp.ControlPlaneError, match="daily limit"):
        mcp.ask_claude({"question": "q"}, request=_claude_reply(), today=lambda: "2026-09-25")
    assert mcp.claude_usage(today=lambda: "2026-09-25")["left_today"] == 0


def test_the_workspace_header_is_sent_when_set(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"content": []}'

    def fake_urlopen(request, timeout):
        sent.update({k.lower(): v for k, v in request.header_items()})
        return Response()

    monkeypatch.setattr(mcp, "urlopen", fake_urlopen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    mcp.claude_request({"model": "m"})
    assert sent["anthropic-workspace-id"] == "wrkspc_123" and sent["x-api-key"] == "sk-ant-test"
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    sent.clear()
    mcp.claude_request({"model": "m"})
    assert "anthropic-workspace-id" not in sent


def test_voice_tools_go_through_the_tts_service():
    calls = []

    def tts(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"current": {"engine": "kitten", "voice": "Bella", "speed": 1.6}, "engines": {}}
        return {"engine": "kokoro", "voice": "af_bella", "speed": 1.0}

    assert mcp.list_voices(request=tts)["current"]["voice"] == "Bella"
    assert mcp.set_voice({"voice": " bella ", "engine": "kokoro"}, request=tts)["now"]["voice"] == "af_bella"
    assert calls[1] == ("POST", "/voice", {"voice": "bella", "engine": "kokoro"})
    with pytest.raises(mcp.ControlPlaneError):
        mcp.set_voice({"voice": " "}, request=tts)
