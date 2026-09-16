# Home-server deployment architecture (k3s across two GPU nodes)

Status: **Phase 2 (cluster bring-up) started, home-server node only.** k3s is
live on the home server (`arch-ssd`, native Arch, not WSL2 — that limitation
was specific to an earlier dev session and no longer applies now that k3s
actually runs here), labeled `gpu-tier=gtx1650`, with `ollama-gtx1650` and
`stt` both `Running` and confirmed doing real CUDA inference inside their
containers (2026-09-16, `kubectl exec ... nvidia-smi` and each pod's own
startup log both show the GTX 1650). **`agent` and `tts` are also applied
and `Running`** (confirmed live via `kubectl -n aicompanion get pods/
deployments` over SSH, 2026-09-16 — this had drifted out of sync with this
doc, which still said "still open" for both; nothing about applying them was
ever hard, they'd just been applied without a doc update). `agent`'s
`OLLAMA_HOST` env is `http://ollama-gtx1650:11434`, confirming the home
server is today's (only) target. `gateway` is **not** applied — nothing in
the cluster speaks to the Stick yet, `bridge_server.py` remains what's
actually flashed against. The RTX 4060 laptop hasn't joined as a second node
yet, and `controller/gpu_scheduler/` is not deployed (still design +
skeleton, per its own README). Treat this doc as the design + the code that
implements it, plus now a partial live result — see each phase's status
line.

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

`bridge_server.py` today does STT -> LLM -> TTS -> speaker-verification ->
WebSocket-to-firmware all in one process (566 lines, see
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
  `bridge_server.py` remains what's actually flashed against for now --
  cutting the Stick over is what's left, tracked in `TODO.md`.

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
| Home server (GTX 1650) | `gpu-tier=gtx1650` | k3s server, `ollama-gtx1650`, `stt`, `tts` (vits), `gateway` |
| Laptop (RTX 4060) | `gpu-tier=rtx4060` | k3s agent, `ollama-rtx4060` (only while the laptop is up) |

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
2. **Cluster bring-up + Helm + ArgoCD** — **partially done**: k3s installed
   and labeled on the home server, GPU runtime chain verified end to end
   (see status section above), `ollama-gtx1650`, `stt`, `tts`, and `agent`
   all applied and `Running` with confirmed GPU access (though `tts`'s
   `/synth` has a known bug, see `TODO.md`). Still open: join the RTX 4060
   laptop as a second node, apply `ollama-rtx4060`/`gateway` (the gateway's
   code now has real firmware parity, see Phase 5 below, but the manifest
   itself isn't applied to the cluster yet), deploy `controller/gpu_scheduler/`
   and validate the node-up/node-down switch against the real two-node
   cluster, chart into `deploy/helm/`, wire `deploy/argocd/` for GitOps sync.
3. **Observability** — `observability/prometheus/` + `observability/grafana/`,
   turning the hand-measured numbers this repo already tracks into live
   dashboards.
4. **Benchmarks** — `benchmarks/latency/` (split-architecture vs.
   `bridge_server.py` monolith) and `benchmarks/gpu_allocation/` (how fast
   the controller actually retargets `agent` on a node transition).
5. **Gateway parity port — mostly done (2026-09-16).** `services/gateway/`
   now speaks `bridge_server.py`'s real firmware wire protocol, the
   speaker-verification gate, `encouragement_loop`, and `announce` -- see
   the service-boundary section above and the module's own docstring.
   Verified against a mocked unit-test suite and a live smoke test against
   the real running `stt`/`agent`/`tts` pods. **Still open**: cutting the
   actual M5StickS3 over from `bridge_server.py` to the k3s-hosted gateway,
   gated on the `tts` bug in Phase 2 and BLE's own Phase 6 soak test. Only
   after that cutover does the k3s deployment become what's actually
   running the device day to day.

Phase 3 and 4 are unstarted; see `TODO.md` for tracking.
