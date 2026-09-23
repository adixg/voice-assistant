#!/usr/bin/env bash
set -u

# Keep the local Prometheus, Grafana and Tempo tunnels alive. The tunnels bind to
# localhost only and are intentionally not exposed outside this machine.
#
# Each tunnel is supervised on its own. They used to share one loop that
# restarted BOTH whenever EITHER exited, which broke as soon as Grafana was
# paused (tools/lean-mode.sh): `kubectl port-forward` waits 60s for a pod, gives
# up, and the shared loop then tore down Prometheus's healthy tunnel too, so
# tools/obs_tui.py lost its data for a few seconds every ~minute.
#
# Plain kubectl (not `sudo k3s kubectl`): the old form asked for sudo once at
# start-up, then needed it again on every restart after sudo's ~5 minute
# timestamp expired, and would have failed silently from then on.
#
#   PROM_PORT=9090 GRAFANA_PORT=3000 TEMPO_PORT=3200 tools/port-forwards.sh

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
NS="${NS:-aicompanion}"
PROM_PORT="${PROM_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
TEMPO_PORT="${TEMPO_PORT:-3200}"

supervise() {
  local svc="$1" local_port="$2" remote_port="$3"
  while true; do
    # A long pod-running timeout makes a tunnel to a paused service wait for it
    # to come back instead of exiting every minute; it still exits (and is
    # restarted after the pause) when the pod it was attached to goes away.
    kubectl -n "$NS" port-forward "svc/$svc" "$local_port:$remote_port" \
      --pod-running-timeout=24h >/dev/null 2>&1 || true
    sleep 3
  done
}

# Ctrl-C / TERM: stop every supervisor and its tunnel, then exit.
trap 'trap - INT TERM EXIT; kill 0 2>/dev/null; exit 0' INT TERM
trap 'kill 0 2>/dev/null' EXIT

supervise prometheus "$PROM_PORT" 9090 &
supervise grafana "$GRAFANA_PORT" 3000 &
supervise tempo "$TEMPO_PORT" 3200 &
wait
