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

   Use the home server's **Tailscale MagicDNS name** for `<home-server>`
   (currently `arch-ssd.<tailnet>.ts.net`, confirm with `tailscale status`
   on that box), not its LAN IP — the LAN address is DHCP-assigned with no
   reservation, so it can change on a lease renewal and silently break this
   join and anything else pointed at it. The home server is a laptop with
   `chassis: laptop` in `hostnamectl` despite being the always-on node, so
   its Wi-Fi address is exactly as unstable as any other laptop's.
3. On each GPU node: install `nvidia-container-toolkit` (on Arch,
   `pacman -S nvidia-container-toolkit`; it's in `extra`, no AUR needed),
   then `systemctl restart k3s` (or `k3s-agent` on the laptop) so k3s
   re-generates its containerd config and picks up `nvidia-container-runtime`
   automatically. Verify with
   `grep -A3 'runtimes.nvidia' /var/lib/rancher/k3s/agent/etc/containerd/config.toml`
   — k3s adds it as an *additional* runtime named `nvidia`, not the default,
   which is why `runtimeclass.yaml` and the `runtimeClassName: nvidia` on
   the GPU pod specs below exist (see that file's comment for the reasoning).
4. Apply `runtimeclass.yaml` and `nvidia-device-plugin.yaml` (below) so pods
   can request `nvidia.com/gpu: 1`.
5. Label each node with which GPU it actually has — nothing in k3s infers
   this automatically:
   ```
   kubectl label node <home-server-hostname> gpu-tier=gtx1650
   kubectl label node <laptop-hostname> gpu-tier=rtx4060
   ```

## Getting the images

`.github/workflows/ci.yml`'s `docker-build` job pushes all five images to
`ghcr.io/adixg/<image>:latest` on every push to `main` (build-only, no push,
on PRs). That's the only place these images get built and hosted -- nothing
builds them on the home server itself.

GHCR images pushed via the workflow's default `GITHUB_TOKEN` are **private**
by default, even though this repo is public. Two ways to let the cluster
pull them, pick one before `kubectl apply`:

- **Make the packages public** (simplest): after
  the first push, on GitHub go to each package under
  github.com/adixg?tab=packages → Package settings → Change visibility →
  Public. One-time, per image.
- **Or create an image pull secret** and reference it from each Deployment:
  ```
  kubectl create secret docker-registry ghcr-pull \
    --docker-server=ghcr.io \
    --docker-username=<your-github-username> \
    --docker-password=<a PAT with read:packages> \
    -n aicompanion
  ```
  then add `imagePullSecrets: [{name: ghcr-pull}]` under each Deployment's
  `spec.template.spec` in `agent.yaml`/`gateway.yaml`/`stt.yaml`/`tts.yaml`
  (not committed by default, since referencing a pull secret that doesn't
  exist yet blocks the pull entirely, even for an otherwise-public image).

## Apply

```
kubectl apply -f namespace.yaml
kubectl apply -f runtimeclass.yaml
kubectl apply -f nvidia-device-plugin.yaml
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
RTX 4060 when present is `controller/gpu_scheduler/`'s job (design +
skeleton code written, not yet run against a live cluster — see that
directory's README for status). Until it's deployed and verified, switching
is a manual `kubectl set env deployment/agent OLLAMA_HOST=http://ollama-rtx4060:11434`.

`stt` and `tts` are pinned to the GTX 1650 node (`nodeSelector: gpu-tier:
gtx1650`) so they're always reachable regardless of whether the laptop is
up; `tts` runs the `vits` backend for that reason too (the only backend
that's free on VRAM — see `docs/hardware-budget.md`). Chatterbox on the
4060 for when it's actually up is future work, not wired in yet.
