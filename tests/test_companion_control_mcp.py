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
        "get_service_health", "get_gpu_status", "get_agent_status", "search_web"]


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
            return prom_result([sample({"Hostname": "arch-ssd", "gpu": "0"}, 73)])
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
