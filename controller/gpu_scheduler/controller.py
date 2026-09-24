"""Routes the `agent` Deployment's llama.cpp endpoint (and matching model) to
whichever GPU node is actually up, and keeps the always-on GTX 1650's standby
model server scaled to zero while the RTX 4060 is doing the work.

The decision is level-triggered: decide() looks at the whole picture (is the
4060 node Ready *and* its llama server Available, how long has that been true,
where `agent` points, is the standby running) and says what to do next. A timer
re-evaluates every few seconds and node events trigger it immediately, so a
missed event or a controller restart heals itself -- no state is kept in
memory. See README.md in this directory for the design.

Run: kopf run controller.py --namespace=aicompanion
"""
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import kopf
from kubernetes import client, config

NAMESPACE = "aicompanion"
AGENT_DEPLOYMENT = "agent"
STANDBY_DEPLOYMENT = "llama-cpp-gtx1650"
RTX4060_DEPLOYMENT = "llama-cpp-rtx4060"
GTX1650_HOST = "http://llama-cpp-gtx1650:8080/v1"
RTX4060_HOST = "http://llama-cpp-rtx4060:8080/v1"
# Each llama-server instance only has the model sized for its own card --
# qwen3.5:4b fits the GTX 1650's 4GB comfortably, qwen3:8b needs the RTX
# 4060's 8188 MiB headroom. Keep in sync with agent.yaml's LLM_MODEL
# default and services/agent/app.py's --model env fallback.
GTX1650_MODEL = "qwen3.5-4b"
RTX4060_MODEL = "qwen3-8b"
GPU_TIER_LABEL = "gpu-tier"
RTX4060_TIER = "rtx4060"
GTX1650_TIER = "gtx1650"

# Annotation on the standby Deployment: "true" pins it running, so failover is
# instant again at the cost of its RAM and VRAM. Set it with tools/standby.sh.
KEEP_WARM_ANNOTATION = "aicompanion/keep-warm"
# The 4060 must have been up this long before the standby is scaled away. A
# laptop that keeps sleeping and waking would otherwise make the 1650 reload its
# model every time. Scaling *up* on a loss is never delayed.
SCALE_DOWN_AFTER_SECONDS = 60
RECONCILE_INTERVAL_SECONDS = 10

_reconcile_lock = threading.Lock()


@dataclass
class State:
    rtx_up: bool                  # 4060 node Ready and its llama server Available
    rtx_up_since: datetime | None
    standby_replicas: int
    standby_ready: bool
    keep_warm: bool
    agent_host: str | None
    agent_rolled_out: bool        # no rollout of the agent in flight


def decide(state: State, now: datetime) -> list[tuple]:
    """The next steps, as ("scale_standby", n) and ("target", "rtx4060"|"gtx1650").

    Ordering is the point: on a loss the standby is brought up first and `agent`
    is only pointed at it once it can answer; on a recovery `agent` is moved to
    the 4060 first and the standby is only scaled away after that rollout
    finished. Either way `agent` never targets a server that isn't there.
    """
    actions = []
    if state.keep_warm and state.standby_replicas == 0:
        actions.append(("scale_standby", 1))

    if not state.rtx_up:
        if state.standby_replicas == 0 and not state.keep_warm:
            actions.append(("scale_standby", 1))
        if state.standby_ready and state.agent_host != GTX1650_HOST:
            actions.append(("target", GTX1650_TIER))
        return actions

    if state.agent_host != RTX4060_HOST:
        actions.append(("target", RTX4060_TIER))
        return actions
    stable = (state.rtx_up_since is not None
              and (now - state.rtx_up_since).total_seconds() >= SCALE_DOWN_AFTER_SECONDS)
    if state.standby_replicas > 0 and not state.keep_warm and state.agent_rolled_out and stable:
        actions.append(("scale_standby", 0))
    return actions


def _is_rtx4060(meta: dict) -> bool:
    return (meta.get("labels") or {}).get(GPU_TIER_LABEL) == RTX4060_TIER


def _set_agent_target(host: str, model: str, logger):
    """Patch the agent Deployment's env so it talks to `host` and requests
    `model`. Patching the pod template spec is itself what triggers a
    rollout -- Kubernetes does the restart, this just states the desired
    values, and the same restart that swaps LLM_HOST picks up the
    matching LLM_MODEL for free."""
    apps = client.AppsV1Api()
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "agent", "env": [
                            {"name": "LLM_HOST", "value": host},
                            {"name": "LLM_MODEL", "value": model},
                        ]}
                    ]
                }
            }
        }
    }
    apps.patch_namespaced_deployment(AGENT_DEPLOYMENT, NAMESPACE, patch)
    logger.info(f"agent LLM_HOST -> {host}, LLM_MODEL -> {model}")


def _condition_since(conditions, kind):
    """When condition `kind` last turned True, or None if it isn't True."""
    for c in conditions or []:
        if c.type == kind and c.status == "True":
            return c.last_transition_time
    return None


def read_state() -> State:
    core, apps = client.CoreV1Api(), client.AppsV1Api()
    nodes = core.list_node(label_selector=f"{GPU_TIER_LABEL}={RTX4060_TIER}").items
    node_since = [t for t in (_condition_since(n.status.conditions, "Ready") for n in nodes) if t]
    rtx = apps.read_namespaced_deployment(RTX4060_DEPLOYMENT, NAMESPACE)
    llama_since = (_condition_since(rtx.status.conditions, "Available")
                   if (rtx.status.ready_replicas or 0) >= 1 else None)
    rtx_up = bool(node_since) and llama_since is not None
    since = max([*node_since, llama_since]) if rtx_up else None

    standby = apps.read_namespaced_deployment(STANDBY_DEPLOYMENT, NAMESPACE)
    agent = apps.read_namespaced_deployment(AGENT_DEPLOYMENT, NAMESPACE)
    agent_env = {e.name: e.value for c in agent.spec.template.spec.containers
                 if c.name == "agent" for e in (c.env or [])}
    want = agent.spec.replicas or 0
    return State(
        rtx_up=rtx_up,
        rtx_up_since=since,
        standby_replicas=standby.spec.replicas or 0,
        standby_ready=(standby.status.ready_replicas or 0) >= 1,
        keep_warm=(standby.metadata.annotations or {}).get(KEEP_WARM_ANNOTATION, "").lower() == "true",
        agent_host=agent_env.get("LLM_HOST"),
        agent_rolled_out=(agent.status.observed_generation == agent.metadata.generation
                          and (agent.status.updated_replicas or 0) == want
                          and (agent.status.ready_replicas or 0) == want
                          and not agent.status.unavailable_replicas),
    )


def _scale_standby(replicas: int, logger):
    client.AppsV1Api().patch_namespaced_deployment(
        STANDBY_DEPLOYMENT, NAMESPACE, {"spec": {"replicas": replicas}})
    logger.info(f"{STANDBY_DEPLOYMENT} replicas -> {replicas}")


def reconcile(logger):
    """Read the cluster, decide, act. Safe to call from any handler at any time."""
    with _reconcile_lock:
        for action, value in decide(read_state(), datetime.now(timezone.utc)):
            if action == "scale_standby":
                _scale_standby(value, logger)
            elif value == RTX4060_TIER:
                _set_agent_target(RTX4060_HOST, RTX4060_MODEL, logger)
            else:
                _set_agent_target(GTX1650_HOST, GTX1650_MODEL, logger)


@kopf.on.startup()
def startup(logger, **_):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    logger.info("gpu_scheduler controller started")


@kopf.on.field("", "v1", "nodes", field="status.conditions")
def on_node_condition_change(meta, logger, **_):
    if _is_rtx4060(meta):  # only the 4060 node's readiness changes routing
        reconcile(logger)


@kopf.on.delete("", "v1", "nodes")
def on_node_delete(meta, logger, **_):
    # The node object itself is gone (e.g. after a clean `kubectl delete node`
    # on laptop shutdown), not just NotReady -- same fallback either way.
    if _is_rtx4060(meta):
        reconcile(logger)


@kopf.timer("", "v1", "nodes", labels={GPU_TIER_LABEL: GTX1650_TIER},
            interval=RECONCILE_INTERVAL_SECONDS, initial_delay=5)
def periodic_reconcile(logger, **_):
    """Runs on the always-on node's object, which always exists: it is what
    notices the 4060's model server dying while its node stays Ready, the
    debounce running out, and the standby becoming ready after a scale-up."""
    reconcile(logger)
