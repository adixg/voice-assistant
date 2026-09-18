# GPU scheduler controller

The differentiated piece of this deployment track: a small custom Kubernetes
controller that routes the `agent` service to whichever Ollama instance is
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
  template env (`OLLAMA_HOST=http://ollama-rtx4060:11434`,
  `OLLAMA_MODEL=qwen3:8b`) in the `aicompanion` namespace. Patching the pod
  template is enough to trigger a rollout on its own -- no separate restart
  call needed.
- On the 4060 node leaving `Ready` (or being deleted, e.g. `kubectl delete
  node` after a clean shutdown): patches `agent` back to
  `OLLAMA_HOST=http://ollama-gtx1650:11434` and
  `OLLAMA_MODEL=qwen3.5:4b`.
- Deliberately narrow scope for now: only `agent` is retargeted. `stt` and
  `tts` stay pinned to the always-on node (see `deploy/kubernetes/tts.yaml`'s
  comment on why chatterbox-on-4060 isn't wired in yet) -- extending the
  controller to also manage a `tts` backend switch is follow-on work once
  that path exists at all.

## Status

**Deployed and verified on the live two-node cluster (2026-09-16).** Both
Ready and NotReady transitions retargeted the agent successfully, including a
real gateway -> agent -> cross-node `ollama-rtx4060` -> `qwen3:8b` inference.
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
ServiceAccount/ClusterRole/ClusterRoleBinding it needs (`get`/`list`/`watch`
on nodes cluster-wide, `get`/`patch` on deployments in the `aicompanion` namespace)
and runs it as a Deployment.

Locally, against whatever kubeconfig context is active:
```
pip install -r requirements.txt
kopf run controller.py --namespace=aicompanion
```
