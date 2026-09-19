# Grafana

`grafana.yaml` provisions Prometheus and Tempo (distributed traces). Tempo
stores traces on the `tempo-data` 5 GiB `local-path` PVC. Change the default
admin password before exposing Grafana beyond a local port-forward.
`dashboard.yaml` provisions the AI Companion service-performance dashboard:
scrape health, request/error rate, HTTP and gateway latency, pod readiness,
container restarts, GPU utilization, VRAM utilization, Tempo traces, and
ElevenLabs usage. GPU and pod panels need
`observability/kubernetes-metrics.yaml` applied first. The dashboard UID is
`aicompanion` and its path is `/d/aicompanion/ai-companion-service-performance`.

```bash
kubectl apply -f observability/grafana/grafana.yaml
kubectl -n aicompanion port-forward svc/grafana 3000:3000
```
