"""Routes the `agent` Deployment's llama.cpp endpoint (and matching model) to
whichever GPU node is actually up, and keeps the always-on GTX 1650's standby
model server scaled to zero while the RTX 4060 is doing the work.

It also lends the idle GTX 1650 to TTS while it isn't needed for the LLM: once
the 4060 has served stably and the standby has left the card, it starts the
`tts-gpu` Deployment (Kokoro on CUDA) and points the `tts` Service at it. On
any loss of the 4060 the Service goes back to the always-running CPU `tts` pod
and `tts-gpu` is scaled to zero, in the same pass that starts the standby, so
the card is free for the LLM.

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

TTS_SERVICE = "tts"
TTS_GPU_DEPLOYMENT = "tts-gpu"
# The `tts` Service's selector picks which TTS pod answers: the CPU one
# (Kitten, always running) or the GPU one.
TTS_SELECTORS = {"cpu": {"app": "tts"}, "gpu": {"app": "tts-gpu"}}
# Annotation on tts-gpu: "off" keeps TTS on the CPU always.
GPU_TTS_ANNOTATION = "aicompanion/gpu-tts"

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
    # Standby pods that still exist, terminating ones included: the 1650's
    # VRAM is only free once this is 0.
    standby_pods: int = 0
    gpu_tts_enabled: bool = False  # tts-gpu exists and isn't annotated off
    gpu_tts_replicas: int = 0
    gpu_tts_ready: bool = False
    tts_target: str = "cpu"        # which pod the tts Service selects


def decide(state: State, now: datetime) -> list[tuple]:
    """TTS steps first, so a loss of the 4060 frees the 1650 in the same pass
    that starts the standby; then the LLM steps (decide_llm)."""
    return decide_tts(state, now) + decide_llm(state, now)


def _rtx_stable(state: State, now: datetime) -> bool:
    return (state.rtx_up and state.rtx_up_since is not None
            and (now - state.rtx_up_since).total_seconds() >= SCALE_DOWN_AFTER_SECONDS)


def decide_tts(state: State, now: datetime) -> list[tuple]:
    """("tts_target", "cpu"|"gpu") and ("scale_gpu_tts", n).

    The GPU voice is used only while the 1650 has nothing else to do: the 4060
    serving stably with `agent` on it, the standby not pinned and fully gone.
    Anything else sends the Service back to the CPU pod first, then frees the
    card. On the way up the Service moves only once tts-gpu is ready."""
    wanted = (state.gpu_tts_enabled and _rtx_stable(state, now) and state.agent_host == RTX4060_HOST
              and not state.keep_warm and state.standby_replicas == 0 and state.standby_pods == 0)
    if not wanted:
        actions = [("tts_target", "cpu")] if state.tts_target != "cpu" else []
        if state.gpu_tts_replicas > 0:
            actions.append(("scale_gpu_tts", 0))
        return actions
    if state.gpu_tts_replicas == 0:
        return [("scale_gpu_tts", 1)]
    if state.gpu_tts_ready and state.tts_target != "gpu":
        return [("tts_target", "gpu")]
    return []


def decide_llm(state: State, now: datetime) -> list[tuple]:
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
    if state.standby_replicas > 0 and not state.keep_warm and state.agent_rolled_out and _rtx_stable(state, now):
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
    standby_pods = len(core.list_namespaced_pod(
        NAMESPACE, label_selector=f"app={STANDBY_DEPLOYMENT}").items)
    try:
        gpu_tts = apps.read_namespaced_deployment(TTS_GPU_DEPLOYMENT, NAMESPACE)
    except client.ApiException as e:
        if e.status != 404:
            raise
        gpu_tts = None  # manifest not applied: TTS stays on the CPU
    selector = core.read_namespaced_service(TTS_SERVICE, NAMESPACE).spec.selector or {}
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
        standby_pods=standby_pods,
        gpu_tts_enabled=gpu_tts is not None and (gpu_tts.metadata.annotations or {}).get(
            GPU_TTS_ANNOTATION, "").lower() != "off",
        gpu_tts_replicas=(gpu_tts.spec.replicas or 0) if gpu_tts else 0,
        gpu_tts_ready=bool(gpu_tts) and (gpu_tts.status.ready_replicas or 0) >= 1,
        tts_target="gpu" if selector == TTS_SELECTORS["gpu"] else "cpu",
    )


def _scale_standby(replicas: int, logger):
    client.AppsV1Api().patch_namespaced_deployment(
        STANDBY_DEPLOYMENT, NAMESPACE, {"spec": {"replicas": replicas}})
    logger.info(f"{STANDBY_DEPLOYMENT} replicas -> {replicas}")


def _scale_gpu_tts(replicas: int, logger):
    client.AppsV1Api().patch_namespaced_deployment(
        TTS_GPU_DEPLOYMENT, NAMESPACE, {"spec": {"replicas": replicas}})
    logger.info(f"{TTS_GPU_DEPLOYMENT} replicas -> {replicas}")


def _set_tts_target(target: str, logger):
    # A merge patch of the one-key selector swaps the value in place; no pod
    # restarts, the Service's endpoints follow within a second or two.
    client.CoreV1Api().patch_namespaced_service(
        TTS_SERVICE, NAMESPACE, {"spec": {"selector": TTS_SELECTORS[target]}})
    logger.info(f"{TTS_SERVICE} Service -> {target}")


def reconcile(logger):
    """Read the cluster, decide, act. Safe to call from any handler at any time."""
    with _reconcile_lock:
        for action, value in decide(read_state(), datetime.now(timezone.utc)):
            if action == "scale_standby":
                _scale_standby(value, logger)
            elif action == "scale_gpu_tts":
                _scale_gpu_tts(value, logger)
            elif action == "tts_target":
                _set_tts_target(value, logger)
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
