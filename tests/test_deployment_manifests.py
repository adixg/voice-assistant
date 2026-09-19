"""Contract tests for deployment and observability configuration."""
from pathlib import Path


ROOT = Path(__file__).parents[1]


def read(path):
    return (ROOT / path).read_text()


def test_agent_manifest_wires_mcp_and_tracing():
    manifest = read("deploy/kubernetes/agent.yaml")
    for value in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "tempo:4317",
        "MCP_SERVER_COMMAND",
        "python /app/tools/companion_control_mcp.py",
        "COMPANION_CONTROL_PROMETHEUS_URL",
        "http://prometheus:9090",
        "COMPANION_CONTROL_AGENT_URL",
        "http://agent:8002",
    ):
        assert value in manifest


def test_http_services_expose_metrics_and_tracing():
    for path, port in (
        ("deploy/kubernetes/gateway.yaml", "8000"),
        ("deploy/kubernetes/stt.yaml", "8001"),
        ("deploy/kubernetes/tts.yaml", "8003"),
    ):
        manifest = read(path)
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in manifest
        assert "name: metrics" in manifest
        assert f"containerPort: {port}" in manifest


def test_prometheus_has_all_targets_and_persistent_storage():
    manifest = read("observability/prometheus/prometheus.yaml")
    for service in ("gateway|stt|agent|tts|kube-state-metrics|dcgm-exporter", "metrics"):
        assert service in manifest
    assert "name: prometheus-data" in manifest
    assert "storage: 5Gi" in manifest
    assert "scrape_interval: 15s" in manifest


def test_tempo_and_grafana_are_provisioned_together():
    tempo = read("observability/tracing.yaml")
    grafana = read("observability/grafana/grafana.yaml")
    dashboard = read("observability/grafana/dashboard.yaml")
    assert "name: tempo-data" in tempo
    assert "storage: 5Gi" in tempo
    assert "url: http://prometheus:9090" in grafana
    assert "url: http://tempo:3200" in grafana
    assert '"uid": "aicompanion"' in dashboard


def test_dcgm_exporter_has_both_gpu_tiers_and_required_metrics_port():
    manifest = read("observability/kubernetes-metrics.yaml")
    assert "values: [gtx1650, rtx4060]" in manifest
    assert "containerPort: 9400" in manifest
    assert "name: metrics" in manifest
