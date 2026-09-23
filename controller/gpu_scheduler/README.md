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

- Watches `Node` objects cluster-wide for the `gpu-tier: rtx4060` label
  transitioning `Ready` <-> not-`Ready`.
- On the 4060 node becoming `Ready`: patches the `agent` Deployment's pod
  template env (`LLM_HOST=http://llama-cpp-rtx4060:8080/v1`,
  `LLM_MODEL=qwen3-8b`) in the `aicompanion` namespace. Patching the pod
  template is enough to trigger a rollout on its own -- no separate restart
  call needed.
- On the 4060 node leaving `Ready` (or being deleted, e.g. `kubectl delete
  node` after a clean shutdown): patches `agent` back to
  `LLM_HOST=http://llama-cpp-gtx1650:8080/v1` and
  `LLM_MODEL=qwen3.5-4b`.
- Deliberately narrow scope for now: only `agent` is retargeted. `stt` and
  `tts` stay pinned to the always-on node -- extending the controller to also
  manage a `tts` backend switch is follow-on work if a 4060-hosted TTS path
  ever exists.
- The serving runtime was migrated from Ollama to llama.cpp on 2026-09-18
  (see `docs/llama-cpp-migration.md`); the controller's contract is unchanged
  apart from the env var and service names above, which is what the
  OpenAI-compatible seam in `agent` was for.

## Status

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
and `get`/`patch` on the `agent` deployment in the `aicompanion` namespace)
and runs it as a Deployment.

Locally, against whatever kubeconfig context is active:
```
pip install -r requirements.txt
kopf run controller.py --namespace=aicompanion
```
