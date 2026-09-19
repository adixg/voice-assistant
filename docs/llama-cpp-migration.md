# llama.cpp serving migration

The Kubernetes path now serves the voice model with CUDA `llama-server`.
The agent remains an OpenAI-compatible chat-completions client, so gateway,
STT, TTS, firmware, and the wire protocol do not change.

| Node | Service | GGUF | Alias | Cache host path |
| --- | --- | --- | --- | --- |
| GTX 1650 | `llama-cpp-gtx1650:8080` | `TheStageAI/Qwen3.5-4B-GGUF` / `Qwen3.5-4B-M-TS-Q4_K_M.gguf` | `qwen3.5-4b` | `/var/lib/aicompanion/llama-cpp-gtx1650` |
| RTX 4060 | `llama-cpp-rtx4060:8080` | `Qwen/Qwen3-8B-GGUF` / `Qwen3-8B-Q4_K_M.gguf` | `qwen3-8b` | `/var/lib/aicompanion/llama-cpp-rtx4060` |

Both use all GPU layers and a 4096-token context. The first startup downloads
about 2.7 GB on the 1650 and 5 GB on the 4060. `/health` intentionally returns
503 until model loading completes; the Deployment readiness probe therefore
withholds the Service endpoint during that interval. The laptop cache is native
Linux storage inside the D:-hosted WSL VHD, never DrvFS.

## Cutover

Run from the machine with the live kubeconfig. Old and new servers cannot
coexist: each requires the node's only GPU.

```bash
cd deploy/kubernetes
kubectl -n aicompanion delete deployment,service ollama-gtx1650 ollama-rtx4060 --ignore-not-found
kubectl apply -f llama-cpp-gtx1650.yaml
kubectl apply -f llama-cpp-rtx4060.yaml
kubectl apply -f agent.yaml
kubectl rollout status deployment/llama-cpp-gtx1650 -n aicompanion --timeout=15m
kubectl rollout status deployment/agent -n aicompanion --timeout=3m
kubectl -n aicompanion run llama-health --rm -it --restart=Never --image=curlimages/curl -- curl -fsS http://llama-cpp-gtx1650:8080/health
```

Then run a real Android relay -> gateway conversation. When the 4060 joins,
confirm the controller patches `agent` to `LLM_HOST=http://llama-cpp-rtx4060:8080/v1`
and repeat the conversation plus BLE soak tests. Retain the Ollama model data
until this validation and latency/VRAM measurement are complete.

## Rollback

Recover the prior manifests from the pre-migration Git revision, apply them,
then set `agent` back to `--llm-backend ollama` with its prior `OLLAMA_HOST`
and `OLLAMA_MODEL` values. Do not delete the existing Ollama cache before the
new path has passed real-device validation.
