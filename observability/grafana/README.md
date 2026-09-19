# Grafana

`grafana.yaml` provisions Prometheus and Tempo (distributed traces); tracing
is stored in an evaluation-only Tempo `emptyDir`. Change the
default admin password before exposing Grafana beyond a local port-forward.
`dashboard.yaml` provisions the AI Companion request-rate, HTTP p95, and
gateway turn/stage p95 dashboard.

```bash
kubectl apply -f observability/grafana/grafana.yaml
kubectl -n aicompanion port-forward svc/grafana 3000:3000
```
