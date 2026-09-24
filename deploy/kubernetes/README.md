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

`.github/workflows/ci.yml` builds and pushes only the images a push to `main`
touches (of eight: stt, tts, tts-vits, tts-kokoro, agent, gateway,
gpu-scheduler, gpu-exporter) to `ghcr.io/adixg/<image>:latest`; PRs build
without pushing. That's the only place these images get built and hosted -- nothing
builds them on the home server itself.

GHCR images pushed via the workflow's default `GITHUB_TOKEN` are **private**
by default, even though this repo is public. Two ways to let the cluster
pull them, pick one before `kubectl apply`:

- **Make the packages public** (what this cluster does: all eight are public;
  simplest): after
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
  `spec.template.spec` in `agent.yaml`/`gateway.yaml`/`stt.yaml`/`tts.yaml`,
  `controller/gpu_scheduler/deploy.yaml` and the `gpu-exporter` DaemonSet in
  `observability/kubernetes-metrics.yaml` (not committed by default, since referencing a pull secret that doesn't
  exist yet blocks the pull entirely, even for an otherwise-public image).

## Apply

First create/update the personal-data Secret. The gateway image deliberately
does not bake a biometric voiceprint or the private user profile into GHCR:

```
kubectl -n aicompanion create secret generic aicompanion-personal-data \
  --from-file=about-me.md=../../memory/about-me.md \
  --from-file=voiceprint.json=../../memory/voiceprint.json \
  --dry-run=client -o yaml | kubectl apply -f -
```

Run that command from `deploy/kubernetes/` after `namespace.yaml` has been
applied and whenever either file changes.

```
kubectl apply -f namespace.yaml
# create/update aicompanion-personal-data here (command above)
kubectl apply -f runtimeclass.yaml
kubectl apply -f priorityclasses.yaml   # before any workload that sets priorityClassName
kubectl apply -f nvidia-device-plugin.yaml
kubectl apply -f llama-cpp-gtx1650.yaml
kubectl apply -f llama-cpp-rtx4060.yaml
kubectl apply -f stt.yaml
kubectl apply -f tts.yaml
kubectl apply -f searxng.yaml
kubectl apply -f agent.yaml
kubectl apply -f gateway.yaml
kubectl apply -f ../../controller/gpu_scheduler/deploy.yaml   # agent routing + standby replicas
kubectl apply -f ../../observability/kubernetes-metrics.yaml  # kube-state-metrics + gpu-exporter
kubectl apply -f ../../observability/prometheus/prometheus.yaml
kubectl apply -f ../../observability/tracing.yaml
kubectl apply -f ../../observability/grafana/grafana.yaml
kubectl apply -f ../../observability/grafana/dashboard.yaml
```

On the *live* cluster, don't re-apply `agent.yaml` wholesale: the controller
patches its `LLM_HOST`/`LLM_MODEL`, and the manifest's defaults would undo that
until the next reconcile.

`llama-cpp-gtx1650.yaml` deliberately has no `replicas:` field: on a fresh install it
starts at one replica, then `controller/gpu_scheduler` owns it and scales it to zero
while the 4060 serves (`tools/standby.sh warm` pins it). Deploy the controller
(`controller/gpu_scheduler/deploy.yaml`) as well, or the standby just stays running.

`searxng.yaml` provides the cluster-internal, API-key-free web search service
used by the agent's `search_web` MCP tool. It is not exposed through a
NodePort; only the agent can reach it at `http://searxng:8080`.

## Primary device path

The day-to-day route is:

`M5StickS3 -> BLE -> Android relay -> ws://arch-ssd.<tailnet>.ts.net:30800 -> gateway -> stt/agent/tts`

Port `30800` is the fixed NodePort in `gateway.yaml`. Version 1.0 of the
Android app migrates its saved endpoint to that host and port once, while
leaving both fields editable. To validate a rollout:

```
kubectl -n aicompanion rollout status deployment/gateway
kubectl -n aicompanion get endpoints gateway
curl http://arch-ssd.<tailnet>.ts.net:30800/health
```

Then tap **Start** in the Android app. The foreground relay automatically
releases stale GATT clients and retries BLE/WebSocket failures. For a local
fallback, run `bridge_server.py`, enter that machine's Tailscale host and
port `8765` in the app, and tap Start; no firmware change is needed.

## Current service wiring

`agent.yaml` points at the GTX 1650's llama.cpp Service by default,
since that node is guaranteed to be up. The deployed
`controller/gpu_scheduler/` controller switches both `LLM_HOST` and
`LLM_MODEL` to the RTX 4060 while that node is Ready, and back to the GTX
1650 when it is not. It also scales `llama-cpp-gtx1650` to 0 once the 4060
has served for 60 s, and back to 1 (about 61 s cold start) when the 4060 is
lost; `tools/standby.sh warm` pins it running. Both transitions and real
cross-node inference were verified on the live cluster; see that directory's
README.

`stt` and `tts` are pinned to the GTX 1650 node (`nodeSelector: gpu-tier:
gtx1650`) so they're always reachable regardless of whether the laptop is
up. `tts` runs KittenTTS nano (voice Bella, speed 1.6) and `stt` runs
Moonshine, both on CPU via sherpa-onnx; `tools/switch-backend.sh` switches
either (Kokoro, Whisper, Parakeet...). The original VITS backend,
Chatterbox, and ElevenLabs remain selectable in local/service configurations.
