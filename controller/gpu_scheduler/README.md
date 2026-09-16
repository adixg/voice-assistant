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
  template env (`OLLAMA_HOST=http://ollama-rtx4060:11434`) in the `aicompanion`
  namespace. Patching the pod template is enough to trigger a rollout on its
  own -- no separate restart call needed.
- On the 4060 node leaving `Ready` (or being deleted, e.g. `kubectl delete
  node` after a clean shutdown): patches `agent` back to
  `OLLAMA_HOST=http://ollama-gtx1650:11434`.
- Deliberately narrow scope for now: only `agent` is retargeted. `stt` and
  `tts` stay pinned to the always-on node (see `deploy/kubernetes/tts.yaml`'s
  comment on why chatterbox-on-4060 isn't wired in yet) -- extending the
  controller to also manage a `tts` backend switch is follow-on work once
  that path exists at all.

## Status

**Design + skeleton only, not yet run against a live cluster** -- this repo
doesn't have a k3s cluster available from the dev machine this was written
on (see `docs/deployment-architecture.md`'s phase notes). `controller.py` is
real code (kopf handlers + the kubernetes client patch calls), not
pseudocode, but it needs to be validated against the actual two-node cluster
before being trusted, the same way every other piece of hardware-facing code
in this repo gets verified against the real device before being called done.

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
