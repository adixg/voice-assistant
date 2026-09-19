# Prometheus

`prometheus.yaml` discovers the named `metrics` ports for the four application
services, `kube-state-metrics`, and DCGM Exporter, and scrapes their `/metrics`
endpoint every 15 seconds. It uses `emptyDir` for an initial, non-durable
deployment; add a PVC before treating historical data as durable.

Apply after the rebuilt service images are running:

```bash
kubectl apply -f observability/prometheus/prometheus.yaml
kubectl -n aicompanion port-forward svc/prometheus 9090:9090
```

Apply the companion collectors too:

```bash
kubectl apply -f observability/kubernetes-metrics.yaml
```

`kube-state-metrics` is intentionally limited to the `aicompanion` namespace
and exposes pod readiness, container restarts, and deployment readiness.
DCGM Exporter runs only on nodes labeled `gpu-tier=gtx1650` or
`gpu-tier=rtx4060`; it exposes NVIDIA utilization and framebuffer metrics.
