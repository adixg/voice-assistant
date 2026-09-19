# Home-server deployment architecture (k3s across two GPU nodes)

Status: **The cluster core is live; repository/device defaults now target its
gateway, with live device cutover verification pending.** `arch-ssd`
(GTX 1650, native Arch): k3s live, labeled `gpu-tier=gtx1650`, with
`ollama-gtx1650`/`stt`/`agent`/`tts` all `Running` and confirmed doing real
CUDA inference inside their containers. `agent`'s `OLLAMA_HOST` env is
`http://ollama-gtx1650:11434`. `tts`'s `/synth` (both TTS backends assumed a
dev machine's conda envs -- see below) is fixed and re-verified live
(2026-09-16): a real request now returns a real, valid synthesized wav.

The RTX 4060 laptop (WSL2/Ubuntu) has **joined as a second node**
(`laptop-2vc40919`), and **the full chain is proven working end to end on
it, live (2026-09-16)**: a real request round-tripped `gateway` -> `agent`
-> `ollama-rtx4060` (this laptop) -> `qwen3:8b` -> a real generated reply,
confirmed via `agent` opening a genuine cross-node TCP connection, not just
its `OLLAMA_HOST` env var flipping correctly. Getting there took finding
and fixing, in order (each one only surfacing once the previous was fixed):
an RBAC gap (`kopf` needs `patch` on nodes for its own bookkeeping, not
just `get`/`list`/`watch` -- without it every node event 403'd before ever
reaching the controller's actual logic); `agent.yaml`/the controller
needing a `nodeSelector` pinning them to the always-up node (confirmed
live: without one, the scheduler placed both on the 4060 node, so stopping
that laptop killed the controller at the exact moment it needed to fail
`agent` over); and the deep one -- k3s's agent-server reverse tunnel *and*
flannel's VXLAN backend both independently defaulting to each node's LAN
IP instead of its Tailscale address (neither node is on the other's LAN),
silently blackholing all cross-node pod traffic until `--node-ip` and
`--flannel-iface=tailscale0` were set on **both** the agent (this laptop)
and the k3s **server** (`arch-ssd`). `agent.yaml` also gained a paired
`OLLAMA_MODEL` env var (`qwen3.5:4b` on the 1650, `qwen3:8b` on the 4060)
the controller now patches alongside `OLLAMA_HOST` on every transition.
`ollama-rtx4060.yaml` bind-mounts this node's existing native Ollama data
dir directly rather than copying models into the pod -- a copy attempt
(`sudo rsync` of the whole ~20GB store) crashed this laptop when it filled
the real ~25GB free on `C:` (WSL2's `df -h` inside Linux had reported
~880GB free, which was the virtual disk's logical cap, not real headroom --
see the check-disk-space-before-bulk-writes lesson). Pod-to-public-internet
egress on this node was re-verified on 2026-09-18 with a temporary pod
pinned to this node: it resolved `registry.ollama.ai` and made an HTTPS
request successfully (the registry's expected bare-URL 404 made BusyBox
`wget` return nonzero). `gateway`
is **applied and `Running`** too, confirmed with the same live wire-protocol
test. The repository's Android endpoint now points at its stable NodePort
route; applying the updated gateway manifest/Secret and a real Stick
conversation are still required before calling the runtime cutover
hardware-verified.

**GPU runtime chain, verified end to end on `arch-ssd`:**
`nvidia-container-toolkit` (installed via pacman) → k3s auto-detects
`nvidia-container-runtime` and registers it as an *additional* containerd
runtime named `nvidia` (not the default — see `deploy/kubernetes/runtimeclass.yaml`'s
comment for why a `RuntimeClass` + `runtimeClassName: nvidia` on each GPU pod
was chosen over hand-editing k3s's generated containerd config) →
`deploy/kubernetes/nvidia-device-plugin.yaml` (upstream `k8s-device-plugin`
v0.20.0 + a time-slicing `ConfigMap`, `replicas: 2`) makes `nvidia.com/gpu`
allocatable. The **time-slicing is required, not optional**: this node has
exactly one physical GPU, and both `ollama-gtx1650` and `stt` request
`nvidia.com/gpu: 1` — without slicing, the second pod sits `Pending`
("Insufficient nvidia.com/gpu") forever, which is what actually happened
before the `ConfigMap` was added. Slicing only serializes CUDA scheduling,
it doesn't partition VRAM — the same sharing `bridge_server.py`'s single
process already does between STT/LLM/TTS on this card today, so it's not a
new risk.

Two other real bugs found only by actually applying this to a live cluster
(CI's `kind`-cluster manifest validation can't catch either, since it has no
GPU and doesn't exercise a rollout): `stt.yaml`'s args
(`--model`/`--device`) didn't match `services/stt/app.py`'s real flags
(`--whisper-model`/`--whisper-device`) — a plain crash-loop, fixed by
correcting the args; and `stt.yaml` was missing `strategy: type: Recreate`,
which the `ollama-*.yaml` Deployments already had for the same reason
(a GPU-constrained node can't satisfy a `RollingUpdate`'s momentary
old+new-both-alive requirement) — without it, any future rollout of `stt`
deadlocks the same way `ollama-*` would have.

Addressing: the home server is a laptop chassis (`hostnamectl` reports
`chassis: laptop`) despite being "the always-on node," on Wi-Fi with a
DHCP (non-reserved) LAN IP — so its Tailscale MagicDNS name
(`arch-ssd.<tailnet>.ts.net`, stable regardless of DHCP) is what the RTX
4060's `K3S_URL` and any other cross-node reference should use, not the LAN
IP. Owner's call: lid-close is not being guarded against (`HandleLidSwitch`
left at its systemd default of `suspend`) since this machine is meant to
stay open and on AC permanently — a deliberate choice, not an oversight.

## Why this exists

The leanest option for a two-node single-user deployment would be plain
`docker-compose`, but the infra is itself part of the project, so real Kubernetes (`k3s`),
a custom controller, and the accompanying documentation are worth the extra
weight as long as they're genuinely used, not present for show.

## Hardware

- **Home server**: NVIDIA GTX 1650, 4GB VRAM, always on. Runs the k3s
  server (control plane) since it's the node guaranteed to be up.
- **This laptop**: NVIDIA RTX 4060, 8188 MiB VRAM (measured, see
  `docs/hardware-budget.md`), present only when the laptop is up. Joins as a
  k3s agent node.

This is a real, not simulated, scheduling problem: something has to decide
which node's Ollama instance the agent talks to, and fall back cleanly when
the 4060 disappears. That's what `controller/gpu_scheduler/` is for.

## Service boundary

The standalone `bridge_server.py` does STT -> LLM -> TTS ->
speaker-verification -> WebSocket-to-firmware all in one process (566 lines, see
`docs/firmware-notes.md` for its event-loop-blocking history). Splitting
along the registry's own boundaries (`voicepipe/registry.py`'s
STT/LLM/TTS/SV protocols) means each service is a thin FastAPI wrapper
around one registry entry, with **no change to any backend** — same
`add_arguments`/`from_args` contract every entrypoint in this repo already
uses.

- `services/stt/` — faster-whisper over HTTP (`POST /transcribe`).
- `services/agent/` — LLM turn-taking over HTTP (`POST /ask`,
  `POST /ask_stream` as NDJSON). With `--llm-backend ollama` this container
  does no GPU work itself; it's a thin client to whichever Ollama instance
  `--host`/`$OLLAMA_HOST` points at. This is the one the GPU scheduler
  controller retargets.
- `services/tts/` — chatterbox/vits over HTTP (`POST /synth`, returns
  base64 wav chunks).
- `services/gateway/` — the M5StickS3's WebSocket peer. **Full wire-protocol
  parity with `bridge_server.py` as of 2026-09-16** — see its own updated
  docstring. It speaks the real `start`/`stop`/`reset` +
  `heard:`/`status:`/`reply:`/binary-audio/`end` protocol (via a small
  `_AsgiWebSocketAdapter` so FastAPI's WebSocket can reuse the exact same
  dispatch logic bridge_server.py's `handle_client`/`_client_loop` use), the
  speaker-verification gate (in-process, not its own service -- see below),
  `announce` + its Unix socket, and `encourage_loop`. `--enroll` mode was
  deliberately not ported; see the module docstring for why that's fine.
  It is now the primary repository/device route. `bridge_server.py` remains
  the port-8765 fallback; the updated APK and manifest still need a live
  Stick verification, tracked in `TODO.md`.

  The speaker gate is the one pipeline stage that stayed in-process rather
  than becoming its own HTTP service: it's CPU-only (ONNX, ~24MB model) and
  sits in the hot path of every single utterance, so paying a network round
  trip for something this cheap isn't worth the service-boundary purity.

**Known cost of this split**: every hop above is now a network call instead
of an in-process function call, on a pipeline where response latency is
literally what the user experiences. `bridge_server.py`'s existing measured
latency numbers (`docs/hardware-budget.md`) are the baseline; nothing has
been measured for the split version yet (`benchmarks/latency/`, Phase 4).
Splitting was still the right call given the goals above, but
it's a real, deliberate tradeoff, not a free one.

## Why k3s, not plain docker-compose or full upstream k8s

- `docker-compose` doesn't do multi-node scheduling at all — it can't
  express "run this on whichever node has a GPU that's currently up,"
  which is the actual problem here.
- Full upstream `k8s` (`kubeadm`, etc.) is more cluster-admin surface than
  two nodes need, and isn't the tool most associated with the "edge/
  heterogeneous hardware" story this deployment actually tells.
- `k3s` is CNCF-conformant real Kubernetes, is the standard tool for exactly
  this topology (edge nodes, intermittent connectivity), and everything
  written against it (manifests, RBAC, the controller) is portable to
  upstream k8s later if that's ever warranted.

## Node -> service placement

| Node | Label | Runs |
|---|---|---|
| Home server (GTX 1650) | `gpu-tier=gtx1650` | k3s server, `ollama-gtx1650`, `stt`, `tts` (vits), `gateway`, `agent`, `gpu-scheduler` |
| Laptop (RTX 4060) | `gpu-tier=rtx4060` | k3s agent, `ollama-rtx4060` (only while the laptop is up) |

`agent` and `gpu-scheduler` are pinned here too (`nodeSelector`, added
2026-09-16 after a live test caught the gap): neither does GPU work itself,
so there's no reason for the scheduler to ever place them on the
intermittent node, and a real test proved it matters -- with no selector,
both landed on the RTX 4060 node, and stopping that laptop's k3s-agent
killed the controller at the exact moment it needed to fail `agent` back
over to `ollama-gtx1650`.

`stt`/`tts`/`gateway` are pinned to the always-on node so the Stick always
has something to talk to. Only the LLM backend the `agent` service calls
changes based on which node is up — see `deploy/kubernetes/README.md` for
the exact manifests and `controller/gpu_scheduler/README.md` for the
automated-switching design.

## Phases

1. **Service split + manifests** (this session) — `services/*`,
   `deploy/kubernetes/*.yaml`, `controller/gpu_scheduler/` design + skeleton
   code, unit tests for all of it (`tests/test_services_*.py`,
   `tests/test_gpu_scheduler_controller.py`), and `.github/workflows/ci.yml`
   (runs the pytest suite, builds all five Docker images, applies every
   manifest to a throwaway `kind` cluster for real server-side schema
   validation, compile-checks the firmware). CI validates the manifests are
   *well-formed*, which is not the same as Phase 2 below — a generic
   GPU-less `kind` cluster can't stand in for the real two-node
   1650/4060 cluster, it just catches typos before they reach real
   hardware.
2. **Cluster bring-up + Helm + ArgoCD** — **the core objective is done**:
   both nodes joined, GPU runtime chain verified end to end on both (real
   `nvidia-smi` output from a scheduled pod, and real `qwen3:8b` inference
   through `ollama-rtx4060`), every service applied and `Running` with
   confirmed GPU access, and the full `gateway` -> `agent` -> Ollama chain
   round-trips a real reply through either node, including the automatic
   node-up/node-down switch (see status section above for the full bug
   chain this took). Still open: chart into `deploy/helm/`, wire
   `deploy/argocd/` for GitOps sync. Pod-to-public-internet egress on the
   laptop node was separately verified on 2026-09-18.
3. **Observability** — `observability/prometheus/` + `observability/grafana/`,
   turning the hand-measured numbers this repo already tracks into live
   dashboards.
4. **Benchmarks** — `benchmarks/latency/` (split-architecture vs.
   `bridge_server.py` monolith) and `benchmarks/gpu_allocation/` (how fast
   the controller actually retargets `agent` on a node transition).
5. **Gateway parity and repository cutover — done (2026-09-18).** `services/gateway/`
   now speaks `bridge_server.py`'s real firmware wire protocol, the
   speaker-verification gate, `encouragement_loop`, and `announce` -- see
   the service-boundary section above and the module's own docstring.
   Verified against a mocked unit-test suite and a live smoke test against
   the real running `stt`/`agent`/`tts` pods. `gateway.yaml` is now applied
   and `Running` too, re-verified with the same wire-protocol test against
   the actual in-cluster pod. The Android endpoint now defaults and one-time
   migrates to `arch-ssd.<tailnet>.ts.net:30800`, and the gateway manifest
   consumes the private profile/voiceprint through a Secret. **Still open**:
   apply those latest deployment changes from a machine with cluster
   credentials, install the APK, and complete the real-device
   conversation/reconnect soak.

Phase 3 and 4 are unstarted; see `TODO.md` for tracking.
