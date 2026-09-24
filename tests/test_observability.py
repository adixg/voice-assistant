"""Regression tests for the Prometheus instrumentation and dashboard contract."""

import json
import textwrap
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.metrics import (
    GATEWAY_STAGE_DURATION,
    GATEWAY_TURN_DURATION,
    GATEWAY_TURNS,
    HTTP_DURATION,
    HTTP_REQUESTS,
    install_http_metrics,
)


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

    assert dashboard["uid"] == "aicompanion"
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


def test_http_metrics_records_duration_with_same_labels_as_requests():
    app = FastAPI()
    install_http_metrics(app, "test-observability-duration")

    @app.get("/ok")
    def ok():
        return {"ok": True}

    assert TestClient(app).get("/ok").status_code == 200
    samples = HTTP_DURATION.collect()[0].samples
    assert any(
        sample.name.endswith("_count")
        and sample.labels["service"] == "test-observability-duration"
        and sample.labels["route"] == "/ok"
        and sample.labels["method"] == "GET"
        and sample.labels["status"] == "200"
        and sample.value >= 1
        for sample in samples
    )


def test_gateway_metrics_keep_outcome_and_stage_labels():
    GATEWAY_TURNS.labels("success").inc()
    GATEWAY_TURN_DURATION.observe(0.01)
    GATEWAY_STAGE_DURATION.labels("stt").observe(0.01)

    turn_samples = GATEWAY_TURNS.collect()[0].samples
    stage_samples = GATEWAY_STAGE_DURATION.collect()[0].samples
    assert any(sample.labels["outcome"] == "success" and sample.value >= 1 for sample in turn_samples)
    assert any(sample.labels["stage"] == "stt" and sample.name.endswith("_count") for sample in stage_samples)


def test_dcgm_counters_file_collects_every_field_the_code_reads():
    """The exporter only collects what its counters file lists; a field used by a
    dashboard/tool but missing there would silently read as no data."""
    import re
    import subprocess
    import yaml
    docs = list(yaml.safe_load_all(open(ROOT / "observability" / "kubernetes-metrics.yaml")))
    counters = next(d for d in docs if d and d.get("kind") == "ConfigMap"
                    and d["metadata"]["name"] == "dcgm-exporter-counters")["data"]["counters.csv"]
    collected = set(re.findall(r"^(DCGM_FI_\w+)", counters, re.M))
    used = set()
    for path in ("tools/obs_tui.py", "tools/companion_control_mcp.py", "observability/grafana/dashboard.yaml"):
        used |= set(re.findall(r"DCGM_FI_[A-Z_]*[A-Z](?![A-Z_])", (ROOT / path).read_text()))  # not "FB_.*" patterns
    used.discard("DCGM_FI_DEV_FB_TOTAL")  # deliberately not queried: see companion_control_mcp.py
    assert used <= collected, f"read but not collected: {sorted(used - collected)}"
