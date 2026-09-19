# Grafana

`grafana.yaml` provisions Prometheus and Tempo (distributed traces); tracing
is stored in an evaluation-only Tempo `emptyDir`. Change the
default admin password before exposing Grafana beyond a local port-forward.
`dashboard.yaml` provisions the AI Companion service-performance dashboard:
scrape health, request/error rate, HTTP and gateway latency, pod readiness,
container restarts, GPU utilization, and VRAM utilization. GPU and pod panels
need `observability/kubernetes-metrics.yaml` applied first.

```bash
kubectl apply -f observability/grafana/grafana.yaml
kubectl -n aicompanion port-forward svc/grafana 3000:3000
```
