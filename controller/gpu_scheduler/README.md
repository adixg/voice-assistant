# GPU scheduler controller

The differentiated piece of this deployment track: a small custom Kubernetes
controller that routes the `agent` service to whichever llama.cpp server
(`llama-cpp-gtx1650` or `llama-cpp-rtx4060`) is
actually available, rather than a human doing `kubectl set env` by hand
every time the laptop's RTX 4060 node comes up or goes to sleep.

## Why this exists

The cluster spans two real, heterogeneous nodes (see
`docs/deployment-architecture.md`): the home server's GTX 1650, always on,
and this laptop's RTX 4060, present only when the laptop is up. Stock
Kubernetes scheduling (`nodeSelector`/affinity) decides *where a pod runs*,
not *which of two already-running backend instances a live service should
call* -- that's an application-level routing decision, which is exactly the
gap a custom controller fills. It's also the one piece here that isn't
"apply a Helm chart": writing a controller against the Kubernetes API is a
real, comparatively uncommon skill to demonstrate.

## Design

Level-triggered rather than event-per-transition: `decide()` (a pure function,
unit-tested without a cluster) takes a snapshot of the whole picture -- is the
4060 node `Ready` *and* `llama-cpp-rtx4060` `Available`, how long has that been
true, which server `agent` points at, whether the standby is running, whether
it is pinned -- and returns the next steps. Node events for the 4060 trigger it
immediately, and a 10-second timer on the always-on node's object re-runs it,
so a missed event, a controller restart, or the 4060's model server dying while
its node stays `Ready` all heal on their own. Nothing is kept in memory; the
"how long has it been up" clock is the Kubernetes condition's own
`lastTransitionTime`.

- **4060 up** (node Ready and its server Available): patch `agent`'s pod
  template env (`LLM_HOST=http://llama-cpp-rtx4060:8080/v1`,
  `LLM_MODEL=qwen3-8b`) -- patching the template is itself what triggers the
  rollout. Only after that rollout has finished **and** the 4060 has been up for
  60s (`SCALE_DOWN_AFTER_SECONDS`) is `llama-cpp-gtx1650` scaled to 0, freeing
  its ~1 GiB of RAM and 3.4 GiB of VRAM on the home server. The 60s debounce is
  what stops a laptop that keeps sleeping and waking from making the 1650
  reload its model each time.
- **4060 lost** (node NotReady or deleted, or its server down): scale the
  standby to 1 immediately, and point `agent` back at it
  (`LLM_HOST=http://llama-cpp-gtx1650:8080/v1`, `LLM_MODEL=qwen3.5-4b`) only
  once it reports ready, so `agent` never targets a server that isn't there.
  From zero this costs a model load, during which turns fail; a warm standby
  fails over as soon as the node is seen down.
- **Manual override**: annotate the standby `aicompanion/keep-warm=true`
  (`tools/standby.sh warm`; `auto` removes it). The controller then starts it if
  needed and never scales it down, trading its RAM/VRAM for instant failover.
- `llama-cpp-gtx1650.yaml` deliberately has no `replicas:` -- this controller
  owns the field, and a manifest value would be re-applied over it.
- Deliberately narrow scope: only `agent` is retargeted. `stt` and `tts` stay
  pinned to the always-on node.
- The serving runtime was migrated from Ollama to llama.cpp on 2026-09-18
  (see `docs/llama-cpp-migration.md`).

## Status

**Standby autoscaling (2026-09-24) is written and unit-tested but not yet deployed or
exercised on the live cluster** -- the measured failover time from scale-zero is
still to be recorded here.

**Deployed and verified on the live two-node cluster (2026-09-16, against the
Ollama-era services it was first built for).** Both Ready and NotReady
transitions retargeted the agent successfully, including a real
gateway -> agent -> cross-node 4060-hosted model inference. Since the
llama.cpp migration (2026-09-18) the live `agent` is confirmed pointed at
`llama-cpp-rtx4060` with `qwen3-8b`; a fresh 4060-down/4060-up failover
timing measurement is still open (see `TODO.md`).
Live testing also exposed and fixed the controller's missing node-patch RBAC,
and the controller and agent are now pinned to the always-on GTX node so they
remain available when the 4060 disappears. The WSL2/Tailscale networking
fixes needed for that cross-node route are recorded in
`docs/deployment-architecture.md`.

## Running it

Built with [kopf](https://kopf.readthedocs.io/), which handles the
watch/retry/resync plumbing so the handler only states what should happen on
a transition, not how to keep a watch loop alive.

In-cluster (the intended way): `deploy.yaml` in this directory creates the
ServiceAccount/ClusterRole/ClusterRoleBinding it needs (`get`/`list`/`watch`/`patch`
on nodes cluster-wide -- `patch` is for kopf's own bookkeeping annotations --
and, in the `aicompanion` namespace, `get` on `agent`/`llama-cpp-gtx1650`/`llama-cpp-rtx4060` and `patch` on `agent` and `llama-cpp-gtx1650` only)
and runs it as a Deployment.

Locally, against whatever kubeconfig context is active:
```
pip install -r requirements.txt
kopf run controller.py --namespace=aicompanion
```
