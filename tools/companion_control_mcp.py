#!/usr/bin/env python3
"""Read-only MCP control plane for AI Companion.

This server deliberately speaks the MCP stdio JSON-RPC transport itself rather
than importing an SDK.  That keeps its runtime small and lets Hermes launch it
with just Python 3.10+ installed.  It has no shell, Kubernetes, filesystem, or
device-write capability: it reads the existing Prometheus and agent HTTP APIs.

Configure the URLs through environment variables.  The defaults work when the
server runs inside the ``aicompanion`` Kubernetes namespace.  For local Hermes,
point ``COMPANION_CONTROL_PROMETHEUS_URL`` at a port-forward instead.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SERVER_NAME = "aicompanion-companion-control"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
Json = dict[str, Any]
FetchJson = Callable[[str], Json]


class ControlPlaneError(RuntimeError):
    """An upstream status endpoint was unavailable or returned invalid data."""


def _env_url(name: str, default: str) -> str:
    return os.environ.get(name, default).rstrip("/")


def _timeout() -> float:
    try:
        value = float(os.environ.get("COMPANION_CONTROL_TIMEOUT_SECONDS", "5"))
    except ValueError:
        return 5.0
    return max(0.1, min(value, 30.0))


def fetch_json(url: str) -> Json:
    """Fetch one JSON document without ever putting an upstream body on stdout."""
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=_timeout()) as response:  # noqa: S310 -- operator-set URLs
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise ControlPlaneError(f"request to {url} failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControlPlaneError(f"request to {url} returned a JSON value, not an object")
    return payload


def prometheus_query(query: str, get_json: FetchJson = fetch_json) -> list[Json]:
    base_url = _env_url("COMPANION_CONTROL_PROMETHEUS_URL", "http://prometheus:9090")
    payload = get_json(f"{base_url}/api/v1/query?{urlencode({'query': query})}")
    if payload.get("status") != "success":
        raise ControlPlaneError(f"Prometheus rejected query: {payload.get('error', 'unknown error')}")
    data = payload.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        raise ControlPlaneError("Prometheus response had no vector result")
    return [item for item in result if isinstance(item, dict)]


def _sample_value(sample: Json) -> float | None:
    value = sample.get("value")
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def service_health(get_json: FetchJson = fetch_json) -> Json:
    """Return scrape health, pod readiness, and recent restarts by stable names."""
    up = prometheus_query(
        'up{service=~"gateway|stt|agent|tts|kube-state-metrics|dcgm-exporter"}', get_json)
    ready = prometheus_query(
        'max by (pod) (kube_pod_status_ready{namespace="aicompanion",condition="true"})', get_json)
    restarts = prometheus_query(
        'sum by (pod, container) (increase(kube_pod_container_status_restarts_total'
        '{namespace="aicompanion"}[1h]))', get_json)

    targets = []
    for sample in up:
        labels = sample.get("metric", {})
        if not isinstance(labels, dict):
            continue
        targets.append({
            "service": labels.get("service", "unknown"),
            "instance": labels.get("instance", "unknown"),
            "up": _sample_value(sample) == 1.0,
        })
    pods = []
    for sample in ready:
        labels = sample.get("metric", {})
        if isinstance(labels, dict):
            pods.append({"pod": labels.get("pod", "unknown"), "ready": _sample_value(sample) == 1.0})
    restarted = []
    for sample in restarts:
        count = _sample_value(sample)
        labels = sample.get("metric", {})
        if count and isinstance(labels, dict):
            restarted.append({
                "pod": labels.get("pod", "unknown"),
                "container": labels.get("container", "unknown"),
                "restarts_last_hour": count,
            })
    return {"targets": targets, "pods": pods, "restarts_last_hour": restarted}


def gpu_status(get_json: FetchJson = fetch_json) -> Json:
    """Return GPU compute and VRAM percentage from DCGM, without pod-level labels."""
    util = prometheus_query("DCGM_FI_DEV_GPU_UTIL", get_json)
    vram = prometheus_query("100 * DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL", get_json)

    def series(samples: list[Json], field: str) -> list[Json]:
        output = []
        for sample in samples:
            labels = sample.get("metric", {})
            if not isinstance(labels, dict):
                continue
            output.append({
                "host": labels.get("Hostname", labels.get("instance", "unknown")),
                "gpu": labels.get("gpu", "unknown"),
                field: _sample_value(sample),
            })
        return output

    utilization = series(util, "utilization_percent")
    vram_utilization = series(vram, "vram_percent")
    return {
        "utilization": utilization,
        "vram_utilization": vram_utilization,
        "message": None if utilization else "No DCGM GPU samples are available yet.",
    }


def agent_status(get_json: FetchJson = fetch_json) -> Json:
    url = _env_url("COMPANION_CONTROL_AGENT_URL", "http://agent:8002") + "/health"
    payload = get_json(url)
    return {"status": payload.get("status"), "backend": payload.get("backend")}


TOOLS: list[Json] = [
    {
        "name": "get_service_health",
        "description": "Read current service scrape health, pod readiness, and container restarts. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_gpu_status",
        "description": "Read NVIDIA GPU utilization and VRAM utilization collected by DCGM. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_agent_status",
        "description": "Read the AI Companion agent health and configured backend class. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]
TOOL_HANDLERS: dict[str, Callable[[], Json]] = {
    "get_service_health": service_health,
    "get_gpu_status": gpu_status,
    "get_agent_status": agent_status,
}


def _tool_result(payload: Json, is_error: bool = False) -> Json:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "isError": is_error,
    }


def _response(request: Json, result: Json | None = None, error: Json | None = None) -> Json | None:
    if "id" not in request:  # JSON-RPC notification: never reply.
        return None
    response: Json = {"jsonrpc": "2.0", "id": request["id"]}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result if result is not None else {}
    return response


def handle_request(request: Json) -> Json | None:
    """Process one JSON-RPC request. Kept pure enough for unit tests."""
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(params, dict):
        return _response(request, error={"code": -32602, "message": "params must be an object"})

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else "2025-06-18"
        return _response(request, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _response(request, {})
    if method == "tools/list":
        return _response(request, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOL_HANDLERS:
            return _response(request, _tool_result({"error": f"unknown tool: {name}"}, True))
        if not isinstance(arguments, dict) or arguments:
            return _response(request, _tool_result({"error": f"{name} takes no arguments"}, True))
        try:
            return _response(request, _tool_result(TOOL_HANDLERS[name]()))
        except ControlPlaneError as exc:
            return _response(request, _tool_result({"error": str(exc)}, True))
    if method is None:
        return _response(request, error={"code": -32600, "message": "missing method"})
    return _response(request, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
            response = handle_request(request)
            if response is not None:
                print(json.dumps(response), flush=True)
        except (ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": f"parse error: {exc}"}}), flush=True)
        except Exception as exc:  # noqa: BLE001 -- never crash the stdio protocol loop
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32603, "message": f"internal error: {exc}"}}), flush=True)


if __name__ == "__main__":
    main()
