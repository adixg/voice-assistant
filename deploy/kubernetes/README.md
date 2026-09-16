# deploy/kubernetes

Raw manifests for the k3s cluster spanning the home server (GTX 1650,
always-on) and this laptop (RTX 4060, present only when it's up). See
`docs/deployment-architecture.md` for the full design and phase plan; this
file is just the how-to-apply.

## Cluster setup (once)

1. Home server: `curl -sfL https://get.k3s.io | sh -` — this becomes the k3s
   server (control plane), since it's the node that's actually always on.
2. Laptop: join as an agent with the token k3s prints on the server —
   `curl -sfL https://get.k3s.io | K3S_URL=https://<home-server>:6443 K3S_TOKEN=<token> sh -`.
   It's fine for this node to be offline most of the time; k3s just marks it
   `NotReady` and nothing gets scheduled there until it rejoins.
3. Install the [NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin)
   on both nodes so pods can request `nvidia.com/gpu: 1`.
4. Label each node with which GPU it actually has — nothing in k3s infers
   this automatically:
   ```
   kubectl label node <home-server-hostname> gpu-tier=gtx1650
   kubectl label node <laptop-hostname> gpu-tier=rtx4060
   ```

## Apply

```
kubectl apply -f namespace.yaml
kubectl apply -f ollama-gtx1650.yaml
kubectl apply -f ollama-rtx4060.yaml
kubectl apply -f stt.yaml
kubectl apply -f tts.yaml
kubectl apply -f agent.yaml
kubectl apply -f gateway.yaml
```

## Current wiring (Phase 1, manual)

`agent.yaml` points `--host` at the GTX 1650's Ollama Service by default,
since that node is the one guaranteed to be up. Switching it to prefer the
RTX 4060 when present is `controller/gpu_scheduler/`'s job — not built yet,
see that directory's README for the design. Until it exists, switching is a
manual `kubectl set env deployment/agent OLLAMA_HOST=http://ollama-rtx4060:11434`
plus a rollout restart.

`stt` and `tts` are pinned to the GTX 1650 node (`nodeSelector: gpu-tier:
gtx1650`) so they're always reachable regardless of whether the laptop is
up; `tts` runs the `vits` backend for that reason too (the only backend
that's free on VRAM — see `docs/hardware-budget.md`). Chatterbox on the
4060 for when it's actually up is future work, not wired in yet.
