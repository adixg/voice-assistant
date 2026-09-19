"""Regression tests for the Prometheus instrumentation and dashboard contract."""

import json
import textwrap
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.metrics import HTTP_REQUESTS, install_http_metrics


ROOT = Path(__file__).parents[1]


def _dashboard_json():
    source = (ROOT / "observability/grafana/dashboard.yaml").read_text()
    block = source.split("  aicompanion.json: |\n", 1)[1].split("\n---\n", 1)[0]
    return json.loads(textwrap.dedent(block))


def _panel(dashboard, title):
    return next(panel for panel in dashboard["panels"] if panel["title"] == title)


def test_dashboard_uses_exported_vram_metrics_and_zero_fallback_for_5xx():
    dashboard = _dashboard_json()
    vram_expr = _panel(dashboard, "VRAM utilization")["targets"][0]["expr"]
    error_expr = _panel(dashboard, "HTTP 5xx error rate")["targets"][0]["expr"]

    assert "DCGM_FI_DEV_FB_TOTAL" not in vram_expr
    for metric in ("DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_FB_FREE", "DCGM_FI_DEV_FB_RESERVED"):
        assert metric in vram_expr
    assert "or" in error_expr
    assert "* 0" in error_expr


def test_dashboard_readiness_panel_is_instant_query():
    dashboard = _dashboard_json()
    target = _panel(dashboard, "Pod readiness")["targets"][0]

    assert target["instant"] is True
    assert 'condition="true"' in target["expr"]


def test_http_metrics_records_error_status():
    app = FastAPI()
    install_http_metrics(app, "test-observability-error")

    @app.get("/boom")
    def real_boom():
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": True}, status_code=500)

    assert TestClient(app).get("/boom").status_code == 500
    samples = HTTP_REQUESTS.collect()[0].samples
    assert any(
        sample.name == "aicompanion_http_requests_total"
        and sample.labels["service"] == "test-observability-error"
        and sample.labels["route"] == "/boom"
        and sample.labels["status"] == "500"
        and sample.value >= 1
        for sample in samples
    )
