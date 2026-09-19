"""Small Prometheus instrumentation shared by the HTTP services.

Metrics use low-cardinality labels only: service, route, method, and status.
Never put transcript text, user IDs, model replies, or pod IDs in labels.
"""
from time import perf_counter

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "aicompanion_http_requests_total", "HTTP requests completed by service",
    ("service", "route", "method", "status"),
)
HTTP_DURATION = Histogram(
    "aicompanion_http_request_duration_seconds", "HTTP request duration by service",
    ("service", "route", "method", "status"),
)
GATEWAY_TURNS = Counter("aicompanion_gateway_turns_total", "Gateway turns completed", ("outcome",))
GATEWAY_TURN_DURATION = Histogram("aicompanion_gateway_turn_duration_seconds", "Gateway full turn duration")
GATEWAY_STAGE_DURATION = Histogram("aicompanion_gateway_stage_duration_seconds", "Gateway downstream stage duration", ("stage",))


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
