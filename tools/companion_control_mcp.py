#!/usr/bin/env python3
"""Read-only MCP control plane for AI Companion.

This server deliberately speaks the MCP stdio JSON-RPC transport itself rather
than importing an SDK. That keeps its runtime small and lets the agent launch it
with just Python 3.10+ installed.  It has no shell, Kubernetes, filesystem, or
device-write capability: it reads the existing Prometheus and agent HTTP APIs.

Configure the URLs through environment variables. The defaults work when the
server runs inside the ``aicompanion`` Kubernetes namespace. For local development,
point ``COMPANION_CONTROL_PROMETHEUS_URL`` at a port-forward instead.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SERVER_NAME = "aicompanion-companion-control"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
Json = dict[str, Any]
FetchJson = Callable[[str], Json]

# A stable campus-center point avoids a broad-city geocoding result for the
# common voice request "weather at Georgia Tech".
GEORGIA_TECH_COORDS = (33.7759, -84.3975)


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
    # DCGM Exporter exposes framebuffer used/free/reserved, but not a
    # DCGM_FI_DEV_FB_TOTAL series on this cluster.  Derive the total from the
    # exported components, matching the Grafana dashboard query.
    vram = prometheus_query(
        "100 * DCGM_FI_DEV_FB_USED / "
        "(DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE + DCGM_FI_DEV_FB_RESERVED)",
        get_json,
    )

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


def model_status(get_json: FetchJson = fetch_json) -> Json:
    """Report the configured route and the model actually served by llama.cpp."""
    host = os.environ.get("LLM_HOST", "").rstrip("/")
    requested = os.environ.get("LLM_MODEL")
    if not host:
        return {"configured": False, "message": "LLM_HOST is not configured."}
    models = get_json(host + "/models")
    entries = models.get("data")
    served = [item.get("id") for item in entries
              if isinstance(item, dict) and isinstance(item.get("id"), str)] \
        if isinstance(entries, list) else []
    route = "rtx4060" if "rtx4060" in host else "gtx1650" if "gtx1650" in host else "unknown"
    return {
        "configured": True,
        "route": route,
        "endpoint": host,
        "requested_model": requested,
        "served_models": served,
        "active_model_matches": requested in served if requested else None,
        "backend": "openai-compatible",
    }


def current_time(arguments: Json) -> Json:
    """Return a precise local date/time without an external API."""
    requested = arguments.get("timezone") or os.environ.get(
        "COMPANION_CONTROL_TIMEZONE", "America/New_York")
    if not isinstance(requested, str) or not requested.strip():
        raise ControlPlaneError("timezone must be a non-empty IANA timezone")
    try:
        zone = ZoneInfo(requested.strip())
    except ZoneInfoNotFoundError:
        raise ControlPlaneError(f"unknown IANA timezone: {requested}") from None
    now = datetime.now(timezone.utc).astimezone(zone)
    return {
        "timezone": requested.strip(),
        "iso": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "time": now.strftime("%I:%M:%S %p"),
        "day_of_week": now.strftime("%A"),
        "utc_offset": now.strftime("%z"),
    }


def search_web(arguments: Json, get_json: FetchJson = fetch_json) -> Json:
    """Search the self-hosted SearXNG instance and return compact sources."""
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ControlPlaneError("search_web requires a non-empty query")
    query = query.strip()
    if len(query) > 600:
        raise ControlPlaneError("search_web query is limited to 600 characters")
    try:
        max_results = int(arguments.get("max_results", 5))
    except (TypeError, ValueError):
        raise ControlPlaneError("max_results must be an integer") from None
    max_results = max(1, min(max_results, 5))
    freshness = arguments.get("freshness")
    if freshness not in (None, "day", "week", "month", "year"):
        raise ControlPlaneError("freshness must be day, week, month, or year")
    params = {"q": query, "format": "json", "categories": "general"}
    if freshness:
        params["time_range"] = freshness
    base_url = _env_url("COMPANION_CONTROL_SEARXNG_URL", "http://searxng:8080")
    payload = get_json(f"{base_url}/search?{urlencode(params)}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ControlPlaneError("SearXNG response had no results list")
    sources = []
    for item in results[:max_results]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        sources.append({
            "title": str(item.get("title", "")),
            "url": item["url"],
            "snippet": str(item.get("content", ""))[:1000],
            "published": item.get("publishedDate"),
            "engines": item.get("engines", []),
        })
    return {"query": query, "results": sources}


def _is_rainy(hour: Json) -> bool:
    """Use probability, precipitation, and WMO weather codes conservatively."""
    try:
        probability = float(hour.get("precipitation_probability") or 0)
        precipitation = float(hour.get("precipitation") or 0)
        rain = float(hour.get("rain") or 0)
        showers = float(hour.get("showers") or 0)
        code = int(hour.get("weather_code") or 0)
    except (TypeError, ValueError):
        return False
    return (probability >= 40 or precipitation >= 0.1 or rain >= 0.1 or showers >= 0.1
            or code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99})


def weather(arguments: Json, get_json: FetchJson = fetch_json) -> Json:
    """Return current conditions and an hourly rain window for a location."""
    location = arguments.get("location")
    if not isinstance(location, str) or not location.strip():
        raise ControlPlaneError("get_weather requires a non-empty location")
    location = location.strip()
    if len(location) > 120:
        raise ControlPlaneError("weather location is limited to 120 characters")
    if "georgia tech" in location.lower() or "georgia institute of technology" in location.lower():
        latitude, longitude = GEORGIA_TECH_COORDS
        place = {"name": "Georgia Tech", "country": "United States"}
    else:
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
            "name": location, "count": 1, "language": "en", "format": "json",
        })
        geo = get_json(geo_url)
        results = geo.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ControlPlaneError(f"no weather location found for {location!r}")
        place = results[0]
        try:
            latitude = float(place["latitude"])
            longitude = float(place["longitude"])
        except (KeyError, TypeError, ValueError):
            raise ControlPlaneError("geocoding returned no usable coordinates") from None
    forecast_url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,apparent_temperature,precipitation,rain,showers,weather_code,wind_speed_10m",
        "hourly": "precipitation_probability,precipitation,rain,showers,weather_code",
        "daily": "precipitation_probability_max,precipitation_sum,rain_sum,showers_sum,weather_code",
        "forecast_days": 7,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",
    })
    forecast = get_json(forecast_url)
    current = forecast.get("current")
    hourly = forecast.get("hourly")
    if not isinstance(current, dict) or not isinstance(hourly, dict):
        raise ControlPlaneError("weather response was missing current or hourly data")
    times = hourly.get("time", [])
    if not isinstance(times, list):
        raise ControlPlaneError("weather response had no hourly times")
    hours = []
    fields = ("precipitation_probability", "precipitation", "rain", "showers", "weather_code")
    for index, timestamp in enumerate(times):
        if not isinstance(timestamp, str):
            continue
        hour = {field: hourly.get(field, [None] * len(times))[index]
                for field in fields if isinstance(hourly.get(field), list) and index < len(hourly[field])}
        hour["time"] = timestamp
        hours.append(hour)
    now = str(current.get("time", ""))
    upcoming = [hour for hour in hours if hour["time"] >= now]
    first_rain = next((hour for hour in upcoming if _is_rainy(hour)), None)
    stop_time = None
    if first_rain:
        start_index = upcoming.index(first_rain)
        dry_run = 0
        for hour in upcoming[start_index + 1:]:
            if _is_rainy(hour):
                dry_run = 0
            else:
                dry_run += 1
                if dry_run >= 2:
                    stop_time = hour["time"]
                    break
    today = now[:10]
    today_hours = [hour for hour in upcoming if hour["time"][:10] == today]
    today_first_rain = next((hour for hour in today_hours if _is_rainy(hour)), None)
    today_stop = None
    if today_first_rain:
        dry_run = 0
        start_index = today_hours.index(today_first_rain)
        for hour in today_hours[start_index + 1:]:
            if _is_rainy(hour):
                dry_run = 0
            else:
                dry_run += 1
                if dry_run >= 2:
                    today_stop = hour["time"]
                    break
    daily = forecast.get("daily") if isinstance(forecast.get("daily"), dict) else {}
    daily_days = daily.get("time", [])
    daily_rain = []
    if isinstance(daily_days, list):
        for index, day in enumerate(daily_days):
            if not isinstance(day, str):
                continue
            def daily_value(name: str) -> Any:
                values = daily.get(name)
                return values[index] if isinstance(values, list) and index < len(values) else None
            probability = daily_value("precipitation_probability_max") or 0
            rain_sum = daily_value("rain_sum") or 0
            showers_sum = daily_value("showers_sum") or 0
            code = daily_value("weather_code") or 0
            try:
                rainy = float(probability) >= 40 or float(rain_sum) >= 0.1 or float(showers_sum) >= 0.1 \
                    or int(code) in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
            except (TypeError, ValueError):
                rainy = False
            daily_rain.append({
                "date": day,
                "rain_expected": rainy,
                "precipitation_probability_max": probability,
                "rain_mm": rain_sum,
                "showers_mm": showers_sum,
            })
    return {
        "location": place.get("name", location),
        "country": place.get("country"),
        "timezone": forecast.get("timezone"),
        "current": current,
        "rain": {
            "raining_now": _is_rainy(current),
            "expected": first_rain is not None,
            "starts": first_rain["time"] if first_rain else None,
            "stops": stop_time,
            "today_expected": today_first_rain is not None,
            "today_starts": today_first_rain["time"] if today_first_rain else None,
            "today_stops": today_stop,
            "stop_note": "Estimated from hourly forecast; conditions can change." if first_rain else None,
        },
        "coordinates": {"latitude": latitude, "longitude": longitude},
        "daily_forecast": daily_rain,
        "source": "Open-Meteo",
    }


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
    {
        "name": "get_model_status",
        "description": "Read the active GPU route, configured Qwen model, and models actually served by llama.cpp. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_time",
        "description": "Return the current local date and time. Uses America/New_York by default; accepts an IANA timezone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA timezone such as America/New_York or UTC."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_web",
        "description": "Search the public web through the local SearXNG service. Returns source URLs and snippets; read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The web search query."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
                "freshness": {"type": "string", "enum": ["day", "week", "month", "year"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_weather",
        "description": "Get current weather, future hourly rain timing, and a seven-day daily forecast using Open-Meteo. Use today_expected/today_stops for today or when-rain-stops questions, and daily_forecast for this-week questions. Free, read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City, region, or postal address."},
            },
            "required": ["location"],
            "additionalProperties": False,
        },
    },
]
TOOL_HANDLERS: dict[str, Callable[[Json], Json]] = {
    "get_service_health": lambda _args: service_health(),
    "get_gpu_status": lambda _args: gpu_status(),
    "get_agent_status": lambda _args: agent_status(),
    "get_model_status": lambda _args: model_status(),
    "get_time": current_time,
    "search_web": search_web,
    "get_weather": weather,
}
NO_ARGUMENT_TOOLS = {"get_service_health", "get_gpu_status", "get_agent_status", "get_model_status"}


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
        if not isinstance(arguments, dict) or (name in NO_ARGUMENT_TOOLS and arguments):
            return _response(request, _tool_result({"error": f"{name} takes no arguments"}, True))
        try:
            return _response(request, _tool_result(TOOL_HANDLERS[name](arguments)))
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
