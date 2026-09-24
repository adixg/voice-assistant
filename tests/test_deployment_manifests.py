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
        "COMPANION_CONTROL_SEARXNG_URL",
        "http://searxng:8080",
    ):
        assert value in manifest


def test_searxng_is_internal_and_json_enabled():
    manifest = read("deploy/kubernetes/searxng.yaml")
    assert "name: searxng" in manifest
    assert "app: searxng" in manifest
    assert "- json" in manifest
    assert "containerPort: 8080" in manifest


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


# --- memory guardrails for the 8GB always-on node (arch-ssd) -----------------

import yaml

ARCH_SSD_MANIFESTS = (
    "deploy/kubernetes/agent.yaml",
    "deploy/kubernetes/gateway.yaml",
    "deploy/kubernetes/llama-cpp-gtx1650.yaml",
    "deploy/kubernetes/searxng.yaml",
    "deploy/kubernetes/stt.yaml",
    "deploy/kubernetes/tts.yaml",
    "controller/gpu_scheduler/deploy.yaml",
    "observability/grafana/grafana.yaml",
    "observability/kubernetes-metrics.yaml",
    "observability/prometheus/prometheus.yaml",
    "observability/tracing.yaml",
)


def arch_ssd_workloads():
    for path in ARCH_SSD_MANIFESTS:
        for doc in yaml.safe_load_all(read(path)):
            if doc and doc.get("kind") in ("Deployment", "DaemonSet"):
                yield path, doc


def test_every_arch_ssd_workload_has_a_memory_limit_and_a_priority_class():
    """arch-ssd has 8GB shared by everything, so an unbounded pod can push the
    whole node into swap. Every workload needs a memory limit (so a leak gets
    the pod restarted) and a priority class (so monitoring is evicted before
    the voice pipeline)."""
    seen = 0
    for path, doc in arch_ssd_workloads():
        seen += 1
        name = doc["metadata"]["name"]
        spec = doc["spec"]["template"]["spec"]
        assert spec.get("priorityClassName") in ("voice-critical", "monitoring"), (path, name)
        for container in spec["containers"]:
            limits = container.get("resources", {}).get("limits", {})
            assert "memory" in limits, (path, name, container["name"])
    assert seen >= 11


def test_priority_classes_rank_voice_above_default_above_monitoring():
    classes = {d["metadata"]["name"]: d["value"]
               for d in yaml.safe_load_all(read("deploy/kubernetes/priorityclasses.yaml"))}
    assert classes["voice-critical"] > 0 > classes["monitoring"]


def test_lean_mode_pause_label_matches_the_dcgm_daemonset_affinity():
    script = read("tools/lean-mode.sh")
    manifest = read("observability/kubernetes-metrics.yaml")
    assert 'PAUSE_LABEL="aicompanion/monitoring-paused"' in script
    assert "key: aicompanion/monitoring-paused" in manifest
    assert "operator: NotIn" in manifest


def test_lean_mode_never_touches_the_voice_pipeline():
    script = read("tools/lean-mode.sh")
    for name in ("llama-cpp", "stt", "tts", "agent", "gateway", "gpu-scheduler"):
        for line in script.splitlines():
            if line.startswith(("UI=", "METRICS=", "SEARCH=")):
                assert name not in line.replace("kube-state-metrics", "")


# Highest memory each pod was actually measured to use (MiB, from the cgroup's
# memory.peak, 2026-09-23/24, ANON where it is known). A limit at or below one
# of these has already OOM-killed the pod once: tts at 1792Mi (idle reading
# ~1.1Gi, real synthesis peak 2.8Gi), tempo at 384Mi (WAL replay on start), and
# llama-cpp-gtx1650 at 2Gi (reclaimable GGUF cache plus 1.06Gi of anon). Add to
# this table when a pod is measured; never lower a limit below its row.
MEASURED_PEAK_MIB = {
    "tts": 2801,
    "tempo": 384,
    "llama-cpp-gtx1650": 2057,
}


def test_no_memory_limit_is_at_or_below_a_measured_peak():
    def mib(quantity):
        quantity = str(quantity)
        for suffix, factor in (("Gi", 1024), ("Mi", 1)):
            if quantity.endswith(suffix):
                return float(quantity[:-2]) * factor
        raise AssertionError(f"unrecognised memory quantity {quantity!r}")

    checked = set()
    for path, doc in arch_ssd_workloads():
        name = doc["metadata"]["name"]
        if name not in MEASURED_PEAK_MIB:
            continue
        limit = mib(doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])
        assert limit > MEASURED_PEAK_MIB[name], (
            f"{name}: limit {limit:.0f}Mi is not above its measured peak {MEASURED_PEAK_MIB[name]}Mi")
        checked.add(name)
    assert checked == set(MEASURED_PEAK_MIB)     # every row is actually enforced


def test_a_pods_request_never_exceeds_its_limit():
    for path, doc in arch_ssd_workloads():
        for container in doc["spec"]["template"]["spec"]["containers"]:
            res = container.get("resources", {})
            request, limit = res.get("requests", {}).get("memory"), res.get("limits", {}).get("memory")
            if request and limit:
                to_mib = lambda q: float(str(q)[:-2]) * (1024 if str(q).endswith("Gi") else 1)  # noqa: E731
                assert to_mib(request) <= to_mib(limit), (path, doc["metadata"]["name"])
