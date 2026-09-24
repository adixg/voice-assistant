"""Tests for the dependency-free companion-control MCP transport and tools."""
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


def test_tools_list_exposes_only_read_only_tools():
    response = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [tool["name"] for tool in response["result"]["tools"]] == [
        "get_service_health", "get_gpu_status", "get_agent_status", "get_model_status",
        "get_time", "search_web", "get_weather"]


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
