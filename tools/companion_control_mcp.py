#!/usr/bin/env python3
"""MCP control plane for AI Companion.

This server deliberately speaks the MCP stdio JSON-RPC transport itself rather
than importing an SDK. That keeps its runtime small and lets the agent launch it
with just Python 3.10+ installed.  It has no shell, Kubernetes or filesystem
access. It reads the existing Prometheus and agent HTTP APIs, and its only
write capability is the Stick's volume and brightness, through the gateway's
token-protected /device/settings API (bounded values, acknowledged by the
device, counted in Prometheus).

Configure the URLs through environment variables. The defaults work when the
server runs inside the ``aicompanion`` Kubernetes namespace. For local development,
point ``COMPANION_CONTROL_PROMETHEUS_URL`` at a port-forward instead.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SERVER_NAME = "aicompanion-companion-control"
SERVER_VERSION = "0.4.0"
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


def gateway_request(method: str, path: str, body: Json | None = None) -> Json:
    """Call the gateway's device-control API with the shared control token."""
    url = _env_url("COMPANION_CONTROL_GATEWAY_URL", "http://gateway:8000") + path
    token = os.environ.get("COMPANION_CONTROL_TOKEN", "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        # The gateway waits up to 5 s for the Stick to confirm a change.
        with urlopen(request, timeout=max(_timeout(), 8.0)) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ControlPlaneError(detail or f"gateway returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise ControlPlaneError(f"request to the gateway failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControlPlaneError("the gateway returned a JSON value, not an object")
    return payload


# The Stick's settings are 0-255 on the wire; people think in percent.
VOLUME_BROWNOUT_RAW = 191  # M5Stack: above this on battery the Stick can reboot


def _to_percent(raw: int) -> int:
    return round(raw * 100 / 255)


def _to_raw(percent: int) -> int:
    return round(max(0, min(100, percent)) * 255 / 100)


def _describe_stick(settings: Json) -> Json:
    volume, brightness = settings["volume"], settings["brightness"]
    described = {
        "volume_percent": _to_percent(volume),
        "brightness_percent": _to_percent(brightness),
        "screen_off": brightness == 0,
        "firmware": settings.get("firmware"),
    }
    battery = settings.get("battery")
    if isinstance(battery, dict):
        described["battery_percent"] = battery.get("percent")
        described["charging"] = battery.get("charging")
        described["battery_volts"] = battery.get("volts")
        reported = battery.get("reported_at")
        if isinstance(reported, (int, float)):
            described["battery_reported_minutes_ago"] = round(
                (datetime.now(timezone.utc).timestamp() - reported) / 60, 1)
    if volume > VOLUME_BROWNOUT_RAW:
        described["note"] = "Volume above 75% can make the Stick reboot when it runs on battery."
    return described


def stick_settings(_args: Json | None = None, request: Callable[..., Json] = gateway_request) -> Json:
    return _describe_stick(request("GET", "/device/settings"))


def _target_percent(arguments: Json, current_percent: int, name: str) -> int:
    percent, change = arguments.get("percent"), arguments.get("change")
    if (percent is None) == (change is None):
        raise ControlPlaneError(f"give exactly one of percent or change for {name}")
    for value in (percent, change):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ControlPlaneError(f"{name} percent/change must be a whole number")
    target = percent if percent is not None else current_percent + change
    return max(0, min(100, target))


def _set_stick(field: str, arguments: Json, request: Callable[..., Json]) -> Json:
    current = request("GET", "/device/settings")
    target = _target_percent(arguments, _to_percent(current[field]), field)
    applied = request("POST", "/device/settings", {field: _to_raw(target)})
    result = _describe_stick(applied)
    result["changed"] = field
    result["previous_percent"] = _to_percent(current[field])
    return result


def set_stick_volume(arguments: Json, request: Callable[..., Json] = gateway_request) -> Json:
    return _set_stick("volume", arguments, request)


def set_stick_brightness(arguments: Json, request: Callable[..., Json] = gateway_request) -> Json:
    return _set_stick("brightness", arguments, request)


# Reminders live in the gateway, which says each one through the Stick when
# it's due; these tools only manage them through its control API.
def _reminder_summary(reminder: Json) -> Json:
    return {key: reminder.get(key) for key in ("id", "text", "when", "repeat")}


def set_reminder(arguments: Json, request: Callable[..., Json] = gateway_request) -> Json:
    body = {key: arguments[key] for key in ("text", "at", "in_minutes", "repeat") if key in arguments}
    return {"set": _reminder_summary(request("POST", "/reminders", body))}


def list_reminders(_args: Json | None = None, request: Callable[..., Json] = gateway_request) -> Json:
    listing = request("GET", "/reminders")
    return {"now": listing.get("now"),
            "reminders": [_reminder_summary(r) for r in listing.get("reminders", [])]}


def cancel_reminder(arguments: Json, request: Callable[..., Json] = gateway_request) -> Json:
    reminder_id = arguments.get("id")
    if not isinstance(reminder_id, str) or not reminder_id.isalnum():
        raise ControlPlaneError("id must be a reminder id from list_reminders")
    return {"cancelled": _reminder_summary(request("DELETE", f"/reminders/{reminder_id}"))}


# One plain-text notes file shared with the owner (~/rina/notes.md on
# arch-ssd, whose directory is mounted into the agent pod). Only this file:
# no other path is ever opened. Writes replace it atomically, so the owner's
# editor and the agent never see half a file.
NOTES_MAX_BYTES = 20_000


def _notes_path() -> str:
    return os.environ.get("COMPANION_CONTROL_NOTES_FILE", "/rina/notes.md")


def _read_notes_text() -> str:
    try:
        with open(_notes_path(), encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ControlPlaneError(f"can't read the notes file: {exc.strerror}") from exc


def _write_notes_text(text: str, path: str | None = None) -> None:
    if len(text.encode("utf-8")) > NOTES_MAX_BYTES:
        raise ControlPlaneError(f"the notes file would pass {NOTES_MAX_BYTES // 1000} KB; "
                                "rewrite it shorter instead")
    path = path or _notes_path()
    directory = os.path.dirname(path) or "."
    try:
        owner = os.stat(directory)
        fd, tmp = tempfile.mkstemp(prefix=".notes-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            # The agent runs as root: hand the file back to whoever owns the
            # directory, so the owner can keep editing it.
            try:
                os.chown(tmp, owner.st_uid, owner.st_gid)
            except PermissionError:
                pass
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError as exc:
        raise ControlPlaneError(f"can't write the notes file: {exc.strerror}") from exc


def _describe_notes(text: str) -> Json:
    return {"bytes": len(text.encode("utf-8")), "lines": len(text.splitlines())}


def _text_argument(arguments: Json) -> str:
    text = arguments.get("text")
    if not isinstance(text, str):
        raise ControlPlaneError("text must be a string")
    return text


def read_notes(_args: Json | None = None) -> Json:
    text = _read_notes_text()
    result = {"text": text, **_describe_notes(text)}
    if not text:
        result["note"] = "The notes file is empty."
    return result


def add_note(arguments: Json) -> Json:
    line = _text_argument(arguments).strip()
    if not line:
        raise ControlPlaneError("nothing to add")
    text = _read_notes_text()
    if text and not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
    _write_notes_text(text)
    return {"added": line, **_describe_notes(text)}


def write_notes(arguments: Json) -> Json:
    text = _text_argument(arguments)
    if text and not text.endswith("\n"):
        text += "\n"
    previous = _read_notes_text()
    if previous and len(previous.encode("utf-8")) <= NOTES_MAX_BYTES:
        # A rewrite can drop anything, and any voice can ask for one while the
        # speaker check is off: keep the version it replaced.
        _write_notes_text(previous, _notes_path() + ".bak")
    _write_notes_text(text)
    return {"written": True, "previous": _describe_notes(previous), **_describe_notes(text)}


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
        'up{service=~"gateway|stt|agent|tts|kube-state-metrics|gpu-exporter"}', get_json)
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
    """Return GPU compute and VRAM percentage, and any active throttling, from gpu-exporter."""
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
                "host": labels.get("hostname", labels.get("instance", "unknown")),
                "gpu": labels.get("gpu", "unknown"),
                field: _sample_value(sample),
            })
        return output

    utilization = series(util, "utilization_percent")
    vram_utilization = series(vram, "vram_percent")
    # Temperature, power and clock per GPU (and its name), so "how hot are my
    # GPUs?" has an answer: without them the model promised to check and didn't.
    sensors: dict[tuple[str, str], Json] = {}
    for query, field in (("DCGM_FI_DEV_GPU_TEMP", "temperature_c"),
                         ("DCGM_FI_DEV_POWER_USAGE", "power_w"),
                         ('aicompanion_gpu_clock_mhz{clock="sm"}', "sm_clock_mhz")):
        for sample in prometheus_query(query, get_json):
            labels = sample.get("metric", {})
            if not isinstance(labels, dict):
                continue
            host = labels.get("hostname", labels.get("instance", "unknown"))
            entry = sensors.setdefault((host, labels.get("gpu", "unknown")), {
                "host": host, "gpu": labels.get("gpu", "unknown"), "name": labels.get("modelName")})
            value = _sample_value(sample)
            entry[field] = None if value is None else round(value, 1)
    # Reasons the clocks are being held down right now (gpu-exporter), minus
    # "gpu_idle", which only means there is nothing to do.
    throttled = prometheus_query('aicompanion_gpu_throttle{reason!="gpu_idle"} == 1', get_json)
    throttling = [{"host": s.get("metric", {}).get("hostname", "unknown"),
                   "gpu": s.get("metric", {}).get("gpu", "unknown"),
                   "reason": s.get("metric", {}).get("reason", "unknown")} for s in throttled]
    return {
        "utilization": utilization,
        "vram_utilization": vram_utilization,
        "sensors": list(sensors.values()),
        "throttling": throttling,
        "message": None if utilization else "No DCGM GPU samples are available yet.",
    }


def agent_status(get_json: FetchJson = fetch_json) -> Json:
    url = _env_url("COMPANION_CONTROL_AGENT_URL", "http://agent:8002") + "/health"
    payload = get_json(url)
    return {"status": payload.get("status"), "backend": payload.get("backend")}


def model_status(_get_json: FetchJson = fetch_json) -> Json:
    """Report the active scheduler route without recursively calling llama.cpp."""
    host = os.environ.get("LLM_HOST", "").rstrip("/")
    requested = os.environ.get("LLM_MODEL")
    if not host:
        return {"configured": False, "message": "LLM_HOST is not configured."}
    route = "rtx4060" if "rtx4060" in host else "gtx1650" if "gtx1650" in host else "unknown"
    return {
        "configured": True,
        "route": route,
        "endpoint": host,
        "requested_model": requested,
        "served_models": [requested] if requested else [],
        "active_model_matches": True if requested else None,
        "source": "agent scheduler configuration",
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
        "description": "Read each NVIDIA GPU's utilization, VRAM use, temperature (C), power draw (W), clock speed and any throttling. Read-only.",
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
_PERCENT_OR_CHANGE = {
    "type": "object",
    "properties": {
        "percent": {"type": "integer", "minimum": 0, "maximum": 100,
                    "description": "Absolute level in percent."},
        "change": {"type": "integer", "minimum": -100, "maximum": 100,
                   "description": "Relative change in percentage points, e.g. 15 for 'a bit louder/brighter', -15 for 'a bit quieter/dimmer'."},
    },
    "additionalProperties": False,
}
TOOLS += [
    {
        "name": "get_stick_settings",
        "description": "Read the M5Stick's current speaker volume and screen brightness (percent), whether the screen is off, its battery level and voltage, whether it's charging, and its firmware version. The Stick is the device you speak through: questions about your battery, volume, screen or firmware mean the Stick, so call this rather than guess.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_stick_volume",
        "description": "Change the M5Stick's speaker volume. Give exactly one of percent (absolute) or change (relative). Returns the level the Stick confirmed.",
        "inputSchema": _PERCENT_OR_CHANGE,
    },
    {
        "name": "set_stick_brightness",
        "description": "Change the M5Stick's screen brightness. Give exactly one of percent (absolute) or change (relative). 0 percent turns the screen off to save battery; tapping a button on the Stick shows it for 10 seconds. Returns the level the Stick confirmed.",
        "inputSchema": _PERCENT_OR_CHANGE,
    },
]
TOOLS += [
    {
        "name": "read_notes",
        "description": "Read the owner's notes file (a plain-text file on the home server that the owner also edits). Use it when asked what's in the notes, or to recall something the owner asked you to remember.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "add_note",
        "description": "Add one line to the end of the owner's notes file, e.g. when asked to note down or remember something. Keeps everything already there.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The line to add."}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_notes",
        "description": "Replace the whole notes file with new text, e.g. to rewrite, reorganize, or remove something from it. Read it first with read_notes: anything not in text is gone. Up to 20 KB.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The file's complete new contents."}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]
TOOLS += [
    {
        "name": "set_reminder",
        "description": "Set a reminder that you will say out loud through the Stick when it's due. Give exactly one of at or in_minutes. For 'at 3' or 'at 9:30 tomorrow' use at; for 'in 20 minutes' use in_minutes. Times are the owner's local time. Returns when it will go off: tell the owner that.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind about, short, e.g. 'call the dentist'."},
                "at": {"type": "string", "description": "Local 24-hour time: 'HH:MM' for the next time the clock shows it, 'tomorrow HH:MM', a weekday like 'monday HH:MM' (the next one), or 'YYYY-MM-DDTHH:MM' only for dates further away (call get_time first for today's date). Prefer the day words: you don't know today's date."},
                "in_minutes": {"type": "number", "minimum": 1, "description": "Minutes from now."},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekly"], "default": "none"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_reminders",
        "description": "List the reminders that are set, soonest first, with their ids, and the current time.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel one reminder by its id (from list_reminders).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The reminder's id."}},
            "required": ["id"],
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
    "get_stick_settings": lambda _args: stick_settings(),
    "set_stick_volume": set_stick_volume,
    "set_stick_brightness": set_stick_brightness,
    "read_notes": read_notes,
    "add_note": add_note,
    "write_notes": write_notes,
    "set_reminder": set_reminder,
    "list_reminders": lambda _args: list_reminders(),
    "cancel_reminder": cancel_reminder,
}
NO_ARGUMENT_TOOLS = {"get_service_health", "get_gpu_status", "get_agent_status", "get_model_status",
                     "get_stick_settings", "read_notes", "list_reminders"}


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
