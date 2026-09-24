#!/usr/bin/env bash
# Switch the live STT or TTS backend, and its memory limit, in one command.
#
#   tools/switch-backend.sh stt whisper      faster-whisper small on the GTX 1650 (manifest default)
#   tools/switch-backend.sh stt parakeet     NVIDIA Parakeet TDT 0.6B v3, CPU
#   tools/switch-backend.sh stt moonshine    Moonshine base, English only, CPU
#   tools/switch-backend.sh tts kokoro       Kokoro af_bella on PyTorch (manifest default)
#   tools/switch-backend.sh tts kokoro-onnx  the same Kokoro voice on onnxruntime, far less RAM
#   tools/switch-backend.sh tts kitten       KittenTTS nano, voice Bella, smallest and fastest
#   tools/switch-backend.sh status           what each service is running now
#
# Extra flags after the preset go to the backend, e.g.
#   tools/switch-backend.sh tts kitten --kitten-voice Luna --kitten-speed 1.2
# (flag list: python -m services.tts.app --help).
#
# Memory limits move with the backend because they differ by up to 10x, and
# arch-ssd has 7 GiB for the whole stack: keeping PyTorch Kokoro's 3 GiB limit
# on kitten would waste it, and giving parakeet whisper's 1 GiB would OOM it.
# Each limit is the measured peak on arch-ssd plus headroom (2026-09-24,
# docs/hardware-budget.md "sherpa-onnx backends"). kokoro-onnx settles at
# ~1.35 GiB in the service, well above a bare process, and stays there.
#
# This patches the live Deployment. `kubectl apply -f deploy/kubernetes/`
# puts the manifest's backend back; to change the default, edit the
# manifest's args and resources instead.
#
# The sherpa-onnx backends download their model on first start (to a hostPath,
# so only once): parakeet ~460 MB, the others 25-140 MB. The first switch to
# one therefore takes longer to become Ready.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
NS=aicompanion

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# preset -> "request limit" and its args (a JSON array body).
preset() {
  case "$1/$2" in
    stt/whisper)      MEM="512Mi 1Gi"
                      ARGS='"--stt-backend","faster-whisper","--whisper-model","small","--whisper-device","cuda"' ;;
    stt/parakeet)     MEM="1Gi 1536Mi";   ARGS='"--stt-backend","parakeet"' ;;
    stt/moonshine)    MEM="512Mi 1Gi";    ARGS='"--stt-backend","moonshine"' ;;
    tts/kokoro)       MEM="1536Mi 3Gi"
                      ARGS='"--tts-backend","kokoro","--kokoro-voice","af_bella","--kokoro-language","a","--kokoro-device","cpu"' ;;
    tts/kokoro-onnx)  MEM="1Gi 2Gi";     ARGS='"--tts-backend","kokoro-onnx","--kokoro-onnx-voice","af_bella"' ;;
    tts/kitten)       MEM="384Mi 768Mi";  ARGS='"--tts-backend","kitten","--kitten-voice","Bella"' ;;
    *) echo "unknown preset: $1 $2" >&2; usage 1 ;;
  esac
}

status() {
  for svc in stt tts; do
    kubectl -n "$NS" get deployment "$svc" -o jsonpath="{.metadata.name}: {.spec.template.spec.containers[0].args}  memory {.spec.template.spec.containers[0].resources.limits.memory}{'\n'}"
  done
}

switch() {
  local svc="$1" name="$2"; shift 2
  preset "$svc" "$name"
  local extra=""
  for arg in "$@"; do extra+=",\"${arg//\"/\\\"}\""; done
  read -r request limit <<<"$MEM"
  local c=/spec/template/spec/containers/0
  kubectl -n "$NS" patch deployment "$svc" --type=json -p "[
    {\"op\":\"replace\",\"path\":\"$c/args\",\"value\":[${ARGS}${extra}]},
    {\"op\":\"replace\",\"path\":\"$c/resources/requests/memory\",\"value\":\"$request\"},
    {\"op\":\"replace\",\"path\":\"$c/resources/limits/memory\",\"value\":\"$limit\"}]"
  kubectl -n "$NS" rollout status deployment "$svc" --timeout=10m
  status
}

case "${1:-}" in
  stt|tts) [[ $# -ge 2 ]] || usage 1; switch "$@" ;;
  status)  status ;;
  -h|--help|"") usage ;;
  *) usage 1 ;;
esac
