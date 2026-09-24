#!/usr/bin/env bash
# Control whether the GTX 1650's standby LLM server (llama-cpp-gtx1650) may be
# scaled to zero while the RTX 4060 is serving.
#
#   tools/standby.sh auto     let controller/gpu_scheduler scale it (default)
#   tools/standby.sh warm     keep it running: instant failover if the laptop
#                             drops, at ~1 GiB RAM and ~3.4 GiB VRAM on arch-ssd
#   tools/standby.sh status   who serves the LLM and what the standby is doing
#
# `auto` scales the standby to 0 once the 4060 has been serving for a minute
# and back to 1 as soon as the 4060 is lost; failover from zero costs a model
# load (the agent is only retargeted once the standby answers).
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
NS=aicompanion
DEP=llama-cpp-gtx1650
KEY="aicompanion/keep-warm"

case "${1:-status}" in
  warm) kubectl -n "$NS" annotate deployment "$DEP" "$KEY=true" --overwrite
        echo "pinned: the controller will start it if needed and never scale it down" ;;
  auto) kubectl -n "$NS" annotate deployment "$DEP" "$KEY-" >/dev/null 2>&1 || true
        echo "auto: the controller decides (scales down after the 4060 has been up 60s)" ;;
  status)
        pin=$(kubectl -n "$NS" get deployment "$DEP" -o "jsonpath={.metadata.annotations.aicompanion/keep-warm}" 2>/dev/null || true)
        echo "mode:     $([ "$pin" = "true" ] && echo "warm (pinned)" || echo auto)"
        echo "standby:  $(kubectl -n "$NS" get deployment "$DEP" -o 'jsonpath={.status.readyReplicas}/{.spec.replicas} ready' 2>/dev/null)"
        echo "agent -> $(kubectl -n "$NS" get deployment agent -o 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="LLM_HOST")].value}')" ;;
  *) sed -n 2,13p "$0"; exit 1 ;;
esac
