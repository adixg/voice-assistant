# Home-server deployment architecture (k3s across two GPU nodes)

Status: **The cluster core is live; repository/device defaults now target its
gateway, with live device cutover verification pending.** `arch-ssd`
(GTX 1650, native Arch): k3s live, labeled `gpu-tier=gtx1650`, with
`llama-cpp-gtx1650`/`stt`/`agent`/`tts` all `Running` and confirmed doing real
CUDA inference inside their containers. `agent`'s `LLM_HOST` env defaults to
`http://llama-cpp-gtx1650:8080/v1` (the controller flips it, with `LLM_MODEL`,
to `llama-cpp-rtx4060`/`qwen3-8b` while the laptop is up). LLM serving moved
from Ollama to llama.cpp on 2026-09-18 (`docs/llama-cpp-migration.md`); the
2026-09-16 bug-chain narrative below is a historical log written against the
Ollama-era `ollama-*`/`OLLAMA_*` names, kept as-is. `tts`'s `/synth` (both TTS backends assumed a
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
which node's LLM server the agent talks to, and fall back cleanly when
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

- `services/stt/` — any STT backend over HTTP (`POST /transcribe`); Moonshine on CPU
  since 2026-09-24, faster-whisper before.
- `services/agent/` — LLM turn-taking over HTTP (`POST /ask`,
  `POST /ask_stream` as NDJSON). This container does no GPU work itself;
  it's a thin OpenAI-compatible client to whichever llama.cpp server
  `$LLM_HOST` (model `$LLM_MODEL`) points at. This is the one the GPU
  scheduler controller retargets. (The Ollama-era equivalent was
  `--llm-backend ollama` with `$OLLAMA_HOST`/`$OLLAMA_MODEL`.)
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
  the port-8765 fallback; the updated APK is installed and the manifest is
  applied, but a live Stick conversation through the gateway still needs
  verifying, tracked in `TODO.md`.

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

## Memory budget on arch-ssd (8GB, shared by everything)

`arch-ssd` has 7.6GiB of RAM (one 8GB stick, one empty slot) and runs the
whole always-on stack plus a desktop session. Measured 2026-09-23 with
everything running: ~340MB available and 1.5GB in swap, and two pods had
already been killed for it (`llama-cpp-gtx1650` OOM-killed once, `tts`
restarted 5 times) before any of the guardrails below existed.

`tts` (Kokoro on CPU) is by far the largest pod and is bigger than it first
looked: 1.6Gi right after start, 2.2Gi after one reply-sized synth, 2.8Gi after
a ~1000-character one, and it does not shrink afterwards (measured through its
own `process_resident_memory_bytes`). Its limit is 3Gi, and a first cut of
1792Mi, sized from an idle reading, OOM-killed it mid-reply. Reducing this is
the most valuable remaining RAM lever.

**Done 2026-09-24:** `tts` now runs KittenTTS nano via sherpa-onnx, with a
peak of 559Mi and a 768Mi limit (`kokoro-onnx`, the same af_bella voice, settles
at ~1.35Gi). `stt` moved off the GPU to Moonshine (571Mi peak). See
`docs/hardware-budget.md`, "sherpa-onnx backends".

**Measured per-pod memory (2026-09-24, arch-ssd).** Read from each container's
cgroup (`memory.peak`, `memory.stat`, sampled once a second where noted); the
"peak" column includes reclaimable page cache, "real" is anonymous memory, which
is what actually causes OOM kills.

| Pod | Real use | Peak | Limit | Notes |
|---|---|---|---|---|
| `tts` | 1.2-1.9 GiB in normal turns | 2.8 GiB (1000-char stress) | 3 GiB | OOM-killed at 1792Mi |
| `llama-cpp-gtx1650` | 0.6 GiB after load, **1.06 GiB after a long generation, never shrinks** | 2.0 GiB at load (1.76 GiB is GGUF page cache) | 3 GiB | OOM-killed at 2Gi mid-request; a standby while the agent uses the laptop's LLM |
| `stt` | ~0.4 GiB | 1.0 GiB (page cache) | 1 GiB | 0.27s of memory stall in 17h, so the limit is not what makes it slow |
| `dcgm-exporter` | ~0.42 GiB | 0.44 GiB | 512Mi | closest small pod to its limit |
| `gateway` / `prometheus` / `searxng` / `gpu-scheduler` / `agent` / `kube-state-metrics` | 47-243 MiB | 47-243 MiB | 128-512Mi | comfortable |

Both OOM kills came from sizing a limit off an idle reading. Measure through
load *and* a long generation/synthesis, and read anon separately from cache.
`tests/test_deployment_manifests.py` keeps a `MEASURED_PEAK_MIB` table so a
limit cannot be lowered to or below a measured peak again.

What this says about the machine: real peaks of the voice pipeline (`tts` up to
~2.8 GiB, LLM ~1.06 GiB anon, `stt` ~0.4 GiB) plus k3s (~0.7 GiB), the small
pods (~1 GiB) and the desktop do not fit 7.6 GiB together in the worst case, so
it leans on swap. The standby `llama-cpp-gtx1650` costs ~0.7-1 GiB of RAM (and
3.4 GiB of VRAM) while the agent points at the laptop; scaling it to 0 while the
4060 is up would recover that at the price of a slower failover. The second RAM
stick remains the real fix.

- **Every workload here has a memory limit and a priority class**
  (`tests/test_deployment_manifests.py` enforces it). `voice-critical`
  (llama-cpp, stt, tts, agent, gateway, gpu-scheduler) outranks the default;
  `monitoring` (grafana, tempo, prometheus, kube-state-metrics, dcgm-exporter,
  searxng) is below it, so monitoring is evicted first. Definitions:
  `deploy/kubernetes/priorityclasses.yaml`.
- **Requests sit near measured use, limits well above it.** A limit that is
  too tight is worse than none: Tempo replays its WAL on each start and a
  384Mi limit OOM-killed it in a crash loop (now 1Gi), so size a limit to the
  startup peak, not the steady state.
- **`tts` and the LLM pods use `strategy: Recreate`**: a rolling update would
  briefly hold two ~1GB copies of the pod on this node.
- **`tools/lean-mode.sh`** pauses optional pods to free RAM on demand (`on`:
  Grafana+Tempo, ~0.2GB, nothing functional lost; `deep`: also Prometheus,
  kube-state-metrics and this node's dcgm-exporter, taking available RAM from
  ~340MB to ~1.1GB, but the agent's status/GPU tools stop answering; `max`:
  also SearXNG, so web search stops; `traces` starts only Tempo, for
  `obs_tui.py --traces`; `off` restores). The voice pipeline is
  never touched. dcgm-exporter is a DaemonSet, so it is paused per node with
  the `aicompanion/monitoring-paused` node label, which its affinity excludes.
- **`tools/obs_tui.py`** is a terminal dashboard that replaces opening Grafana
  in a browser day to day: host RAM/swap/memory-pressure (from `/proc`), GPU
  util/VRAM/temp/power, per-pod readiness/restarts/OOM kills/memory vs limit,
  a role per pod (`pipeline`/`support`/..., and for the llama servers `serving`
  vs `standby`, taken from the agent's `aicompanion_agent_llm_target_info`
  metric) plus a dim row for each deployment scaled to 0,
  the speaker-verification gate (state, accepted / rejected / too-short /
  unchecked counts, average scores, how many acceptances sit within 0.1 of the
  threshold, last score; `--speaker-window`, default 24h), and gateway turn +
  per-stage + per-route latency, each toggled with
  `--NAME`/`--no-NAME` or `--only a,b`, with sparkline trend graphs beside the
  bars (`--history 15m`, `--no-graphs`): GPU util/VRAM and service memory from
  Prometheus history, host RAM/swap from live samples. Latency has none, since
  turns are too sparse for a trend. Standard library only; one full frame
  measured 26MiB peak RSS, versus ~500MB for Firefox plus the Grafana pod. It
  reads Prometheus (so lean-mode `on` or lighter, not `deep`); traces in Tempo
  are not shown by default: `--traces` adds a panel of the last few Stick
  turns from Tempo split into stt / agent / tts, which needs Tempo running
  (`tools/lean-mode.sh traces`) and is off by default. It uses the servers'
  own spans, because the gateway's client span for a streaming call closes when
  the response starts (5ms for the agent, whose real time is 8s). A live Stick
  connection is one long-lived WebSocket trace holding many turns, so turns are
  split at each `/transcribe`. Per-container memory does not exist in
  Prometheus here (only `process_resident_memory_bytes` for the four Python
  services), so the pods panel shows RSS vs limit for those and just the limit
  for the rest.
- **`tools/port-forwards.sh` supervises each tunnel separately** (Prometheus,
  Grafana, Tempo). It used to restart both whenever either exited, and
  `kubectl port-forward` gives up after 60s waiting for a pod, so once lean mode
  paused Grafana, Prometheus's healthy tunnel was torn down every ~64s and the
  dashboard flashed errors. It now waits (`--pod-running-timeout=24h`) and uses
  plain `kubectl` instead of `sudo`, which would have needed a password again
  after sudo's timeout. `obs_tui.py` also keeps a panel's last data, marked
  stale, for 60s through a brief outage.
- Not applied via whole-manifest `kubectl apply`: `agent` (the GPU scheduler
  retargets its `LLM_HOST`/`LLM_MODEL` live, so a full apply would undo that)
  and `llama-cpp-gtx1650` (its manifest carries `--jinja` and a readiness probe
  that were never applied live). Their limits were patched onto the live
  objects with `kubectl set resources` instead.

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
| Home server (GTX 1650) | `gpu-tier=gtx1650` | k3s server, `llama-cpp-gtx1650`, `stt` (Moonshine CPU), `tts` (Kitten CPU), `gateway`, `agent`, `gpu-scheduler` |
| Laptop (RTX 4060) | `gpu-tier=rtx4060` | k3s agent, `llama-cpp-rtx4060` (only while the laptop is up) |

`agent` and `gpu-scheduler` are pinned here too (`nodeSelector`, added
2026-09-16 after a live test caught the gap): neither does GPU work itself,
so there's no reason for the scheduler to ever place them on the
intermittent node, and a real test proved it matters -- with no selector,
both landed on the RTX 4060 node, and stopping that laptop's k3s-agent
killed the controller at the exact moment it needed to fail `agent` back
over to the GTX 1650's LLM server.

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
   `nvidia-smi` output from a scheduled pod, and real `qwen3:8b` inference (Ollama-era; now llama.cpp)
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
   consumes the private profile/voiceprint through a Secret. Those deployment
   changes are applied and the APK is installed (verified/confirmed
   2026-09-23). **Still open**: complete the real-device
   conversation/reconnect soak.

Phase 3 (observability) is deployed, with dashboard polish pending; Phase 4
(measured benchmarks) is still open; see `TODO.md` for tracking.
