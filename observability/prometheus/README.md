# Prometheus

`prometheus.yaml` discovers the four named service ports and scrapes their
`/metrics` endpoint every 15 seconds. It uses `emptyDir` for an initial,
non-durable deployment; add a PVC before treating historical data as durable.

Apply after the rebuilt service images are running:

```bash
kubectl apply -f observability/prometheus/prometheus.yaml
kubectl -n aicompanion port-forward svc/prometheus 9090:9090
```
