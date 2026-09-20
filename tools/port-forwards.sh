#!/usr/bin/env bash
set -u

# Keep the local Prometheus and Grafana tunnels alive. The tunnels bind to
# localhost only and are intentionally not exposed outside this machine.

if ! sudo -v; then
  echo "Unable to obtain sudo credentials" >&2
  exit 1
fi

cleanup() {
  kill "${PROM_PID:-}" "${GRAFANA_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while true; do
  sudo k3s kubectl -n aicompanion port-forward svc/prometheus 9090:9090 &
  PROM_PID=$!

  sudo k3s kubectl -n aicompanion port-forward svc/grafana 3000:3000 &
  GRAFANA_PID=$!

  # Restart both tunnels if either one exits (for example after a pod restart).
  wait -n "$PROM_PID" "$GRAFANA_PID" || true

  cleanup
  sleep 3
done
