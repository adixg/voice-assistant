# Home-server deployment architecture (k3s across two GPU nodes)

Status: **directory scaffold, service code, and k8s manifests written; not
yet applied to a live cluster.** This dev session has neither a k3s cluster
nor a Docker daemon reachable from WSL2 (Docker Desktop's WSL integration
isn't enabled for this distro — `docker` resolves to the Windows binary, not
a usable daemon here), so nothing below has been build-tested or run
end-to-end yet. Treat this doc as the design + the code that implements it,
not as a "done" report — see each phase's status line.

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
- `services/gateway/` — the M5StickS3's WebSocket peer. **Phase 1 skeleton
  only** — see its own docstring. It proves the stt->agent->tts chain works
  over HTTP, but does not yet speak the firmware's real wire protocol or
  carry over speaker verification, the encouragement loop, or proactive
  `announce`. `bridge_server.py` remains what's actually flashed against
  until that port is done (Phase 2, tracked in `TODO.md`).

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
2. **Cluster bring-up + Helm + ArgoCD** — actually install k3s on both
   nodes, label them, apply the manifests, validate the gateway's Phase-1
   WS endpoint against a test client, chart the manifests into
   `deploy/helm/`, wire `deploy/argocd/` for GitOps sync.
3. **Observability** — `observability/prometheus/` + `observability/grafana/`,
   turning the hand-measured numbers this repo already tracks into live
   dashboards.
4. **Benchmarks** — `benchmarks/latency/` (split-architecture vs.
   `bridge_server.py` monolith) and `benchmarks/gpu_allocation/` (how fast
   the controller actually retargets `agent` on a node transition).
5. **Gateway parity port** — port `bridge_server.py`'s real firmware wire
   protocol, speaker-verification gate, encouragement loop, and `announce`
   into `services/gateway/`, then cut the actual M5StickS3 over from
   `bridge_server.py` to the k3s-hosted gateway. Only after this phase does
   the k3s deployment become what's actually running the device day to day.

Phases 2-5 are unstarted; see `TODO.md` for tracking.
