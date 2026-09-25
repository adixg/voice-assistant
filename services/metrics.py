"""Small Prometheus instrumentation shared by the HTTP services.

Metrics use low-cardinality labels only: service, route, method, and status.
Never put transcript text, user IDs, model replies, or pod IDs in labels.
"""
from time import perf_counter

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "aicompanion_http_requests_total", "HTTP requests completed by service",
    ("service", "route", "method", "status"),
)
HTTP_DURATION = Histogram(
    "aicompanion_http_request_duration_seconds", "HTTP request duration by service",
    ("service", "route", "method", "status"),
)
GATEWAY_TURNS = Counter("aicompanion_gateway_turns_total", "Gateway turns completed", ("outcome",))
# Every device-settings change requested through the gateway's control API
# (volume/brightness, from the agent's MCP tools), by result: the audit count.
DEVICE_SETTINGS_CHANGES = Counter("aicompanion_gateway_device_settings_changes_total",
                                  "Device settings changes requested", ("result",))
# The Stick's battery, as it last reported it (once a minute while connected,
# via the phone's "battery:" report). The report time shows when it went stale.
STICK_BATTERY_PERCENT = Gauge("aicompanion_stick_battery_percent", "Stick battery level (%)")
STICK_BATTERY_VOLTS = Gauge("aicompanion_stick_battery_volts", "Stick battery voltage (V)")
STICK_CHARGING = Gauge("aicompanion_stick_charging", "1 while the Stick is charging")
STICK_BATTERY_REPORT_TIME = Gauge("aicompanion_stick_battery_report_timestamp_seconds",
                                  "When the Stick last reported its battery (unix time)")
# NaN, not 0, until the first report: 0 would plot as a flat battery.
for _gauge in (STICK_BATTERY_PERCENT, STICK_BATTERY_VOLTS, STICK_CHARGING, STICK_BATTERY_REPORT_TIME):
    _gauge.set(float("nan"))
GATEWAY_TURN_DURATION = Histogram("aicompanion_gateway_turn_duration_seconds", "Gateway full turn duration")
GATEWAY_STAGE_DURATION = Histogram("aicompanion_gateway_stage_duration_seconds", "Gateway downstream stage duration", ("stage",))

# Which LLM server the agent is actually calling. The gpu_scheduler controller
# retargets the agent between llama.cpp servers, and kube-state-metrics can't
# see a container's env, so the agent reports it itself; tools/obs_tui.py uses
# it to label the llama pods "serving" or "standby".
AGENT_LLM_TARGET = Gauge("aicompanion_agent_llm_target_info",
                         "1 for the LLM server URL and model the agent is configured to call",
                         ("host", "model"))


def set_agent_llm_target(host: str, model: str) -> None:
    AGENT_LLM_TARGET.clear()
    AGENT_LLM_TARGET.labels(host, model).set(1)


# Speaker-verification gate. Scores are cosine similarities; the buckets are
# dense around the usual 0.6 threshold because the useful question is how close
# to the line accepted turns land (an owner scoring 0.61 against 0.6 is one bad
# day from being locked out). tools/obs_tui.py keeps a copy of these bounds and
# a test keeps the two in step, since that tool can't import this module.
SPEAKER_SCORE_BUCKETS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9, 1.0)
# "unverified" = accepted without being scored: the gate is off, or a clip too
# short to score was let through by --short-utterances allow. Counting it
# separately is what makes that hole visible instead of hiding inside "accepted".
SPEAKER_VERDICTS = ("accepted", "rejected", "too_short", "check_failed", "unverified")
SPEAKER_CHECKS = Counter("aicompanion_gateway_speaker_checks_total",
                         "Speaker-gate verdicts", ("verdict",))
SPEAKER_SCORE = Histogram("aicompanion_gateway_speaker_score",
                          "Speaker-gate cosine score, for checks that produced one",
                          ("verdict",), buckets=SPEAKER_SCORE_BUCKETS)
SPEAKER_LAST_SCORE = Gauge("aicompanion_gateway_speaker_last_score", "Score of the most recent scored check")
SPEAKER_LAST_CHECK = Gauge("aicompanion_gateway_speaker_last_check_timestamp_seconds",
                           "Unix time of the most recent speaker-gate verdict")
SPEAKER_THRESHOLD = Gauge("aicompanion_gateway_speaker_threshold", "Score an utterance must reach to be accepted")
SPEAKER_ENABLED = Gauge("aicompanion_gateway_speaker_gate_enabled",
                        "1 when a voiceprint and backend are configured")
SPEAKER_READY = Gauge("aicompanion_gateway_speaker_gate_ready",
                      "1 when the speaker checker can actually run; 0 with the gate enabled means "
                      "every utterance is being refused (or let through, with --speaker-on-error allow)")
# Pre-create every series at zero so increase() counts the very first event; a
# counter that only appears at 1 shows an increase of 0 over that window.
for _verdict in SPEAKER_VERDICTS:
    SPEAKER_CHECKS.labels(_verdict)
    SPEAKER_SCORE.labels(_verdict)


def record_speaker_check(verdict: str, score, gate) -> None:
    """Record one gate verdict, and refresh the gate's state gauges from `gate`."""
    SPEAKER_CHECKS.labels(verdict).inc()
    SPEAKER_LAST_CHECK.set_to_current_time()
    if score is not None:
        SPEAKER_SCORE.labels(verdict).observe(score)
        SPEAKER_LAST_SCORE.set(score)
    set_speaker_gate_state(gate)


def set_speaker_gate_state(gate) -> None:
    status = gate.status()
    SPEAKER_ENABLED.set(1 if status["enabled"] else 0)
    SPEAKER_READY.set(1 if status["ready"] else 0)
    SPEAKER_THRESHOLD.set(gate.threshold)


def install_http_metrics(app, service: str):
    @app.middleware("http")
    async def observe(request: Request, call_next):
        started = perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            labels = (service, path, request.method, str(status))
            HTTP_REQUESTS.labels(*labels).inc()
            HTTP_DURATION.labels(*labels).observe(perf_counter() - started)

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
