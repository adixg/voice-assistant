# Grafana

`grafana.yaml` provisions Prometheus and Tempo (distributed traces). Tempo
stores traces on the `tempo-data-arch` 5 GiB `local-path` PVC pinned to
`arch-ssd`. The admin password comes from a Secret, which must exist first:
`kubectl -n aicompanion create secret generic grafana-admin --from-literal=password=<choose-one>`.
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
