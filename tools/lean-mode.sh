#!/usr/bin/env bash
# Free RAM on arch-ssd by pausing optional pods, and put them back.
#
#   tools/lean-mode.sh on       pause dashboards + traces   (~0.2 GB, safe)
#   tools/lean-mode.sh deep     also pause metrics          (~0.7 GB total)
#   tools/lean-mode.sh max      also pause web search       (~0.8 GB total)
#   tools/lean-mode.sh dashboards  resume just Grafana + Tempo, leave deeper levels as-is
#   tools/lean-mode.sh off      bring everything back
#   tools/lean-mode.sh status   what's running, plus free memory
#
# Levels are cumulative. Sizes are measured, 2026-09-23. The voice pipeline
# itself (llama-cpp, stt, tts, agent, gateway) is never touched.
#
# What each level costs you:
#   on    Nothing functional. Grafana is UI only; Tempo only receives traces,
#         so services just log failed trace exports while it's down.
#   deep  The agent's status/GPU tools (tools/companion_control_mcp.py) query
#         Prometheus, kube-state-metrics and dcgm-exporter, so "how is the
#         GPU doing" style questions stop working. dcgm-exporter is paused on
#         this node only, the laptop node's keeps running.
#   max   The agent's web search (SearXNG) stops working.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
NS=aicompanion
NODE="${LEAN_NODE:-arch-ssd}"
PAUSE_LABEL="aicompanion/monitoring-paused"

UI=(grafana tempo)
METRICS=(prometheus kube-state-metrics)
SEARCH=(searxng)

kc() { kubectl -n "$NS" "$@"; }

pause() {
  local -a names=("$@")
  [[ ${#names[@]} -eq 0 ]] && return
  kc scale deployment "${names[@]}" --replicas=0
}

pause_dcgm() {
  # dcgm-exporter is a DaemonSet, so it can't be scaled to 0. Its affinity
  # (observability/kubernetes-metrics.yaml) excludes nodes with this label.
  kubectl label node "$NODE" "${PAUSE_LABEL}=true" --overwrite
}

restore() {
  kc scale deployment "${UI[@]}" "${METRICS[@]}" "${SEARCH[@]}" --replicas=1
  kubectl label node "$NODE" "${PAUSE_LABEL}-" >/dev/null 2>&1 || true
}

status() {
  echo "== optional pods (namespace $NS) =="
  kc get deployment "${UI[@]}" "${METRICS[@]}" "${SEARCH[@]}" \
    -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas
  echo
  echo "== dcgm-exporter on $NODE =="
  if [[ "$(kubectl get node "$NODE" -o "jsonpath={.metadata.labels.aicompanion/monitoring-paused}" 2>/dev/null \
      || true)" == "true" ]]; then echo "paused (label ${PAUSE_LABEL}=true)"; else echo "running"; fi
  echo
  echo "== memory on this machine =="
  free -h | sed -n '1,3p'
}

case "${1:-status}" in
  on)   pause "${UI[@]}" ;;
  deep) pause "${UI[@]}" "${METRICS[@]}"; pause_dcgm ;;
  max)  pause "${UI[@]}" "${METRICS[@]}" "${SEARCH[@]}"; pause_dcgm ;;
  dashboards) kc scale deployment "${UI[@]}" --replicas=1 ;;
  off)  restore ;;
  status) ;;
  *) sed -n '2,11p' "$0"; exit 2 ;;
esac

# Give the cluster a few seconds to actually stop/start pods before reporting.
[[ "${1:-status}" != status ]] && sleep 8
status
