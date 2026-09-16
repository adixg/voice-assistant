"""Retargets the `agent` Deployment's Ollama endpoint (and matching model)
to whichever GPU node is actually up. See README.md in this directory for
the design and current status -- validated live against the real two-node
cluster (2026-09-16), including the actual failover in both directions.

Run: kopf run controller.py --namespace=aicompanion
"""
import kopf
from kubernetes import client, config

NAMESPACE = "aicompanion"
AGENT_DEPLOYMENT = "agent"
GTX1650_HOST = "http://ollama-gtx1650:11434"
RTX4060_HOST = "http://ollama-rtx4060:11434"
# Each Ollama instance only has the model sized for its own card pulled --
# qwen3.5:4b fits the GTX 1650's 4GB comfortably, qwen3:8b needs the RTX
# 4060's 8188 MiB headroom. Keep in sync with agent.yaml's OLLAMA_MODEL
# default and services/agent/app.py's --model env fallback.
GTX1650_MODEL = "qwen3.5:4b"
RTX4060_MODEL = "qwen3:8b"
GPU_TIER_LABEL = "gpu-tier"
RTX4060_TIER = "rtx4060"


def _is_rtx4060(meta: dict) -> bool:
    return (meta.get("labels") or {}).get(GPU_TIER_LABEL) == RTX4060_TIER


def _conditions_are_ready(conditions: list) -> bool:
    """`conditions` is a Node's status.conditions list as kopf hands it to a
    field handler -- plain dicts, not the typed V1NodeCondition the
    kubernetes client uses elsewhere in this file."""
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (conditions or []))


def _set_agent_target(host: str, model: str, logger):
    """Patch the agent Deployment's env so it talks to `host` and requests
    `model`. Patching the pod template spec is itself what triggers a
    rollout -- Kubernetes does the restart, this just states the desired
    values, and the same restart that swaps OLLAMA_HOST picks up the
    matching OLLAMA_MODEL for free."""
    apps = client.AppsV1Api()
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "agent", "env": [
                            {"name": "OLLAMA_HOST", "value": host},
                            {"name": "OLLAMA_MODEL", "value": model},
                        ]}
                    ]
                }
            }
        }
    }
    apps.patch_namespaced_deployment(AGENT_DEPLOYMENT, NAMESPACE, patch)
    logger.info(f"agent OLLAMA_HOST -> {host}, OLLAMA_MODEL -> {model}")


@kopf.on.startup()
def startup(logger, **_):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    logger.info("gpu_scheduler controller started")


@kopf.on.field("", "v1", "nodes", field="status.conditions")
def on_node_condition_change(meta, status, logger, **_):
    if not _is_rtx4060(meta):
        return  # only the 4060 node's readiness changes agent routing today

    ready = _conditions_are_ready(status.get("conditions"))
    if ready:
        _set_agent_target(RTX4060_HOST, RTX4060_MODEL, logger)
    else:
        _set_agent_target(GTX1650_HOST, GTX1650_MODEL, logger)


@kopf.on.delete("", "v1", "nodes")
def on_node_delete(meta, logger, **_):
    if not _is_rtx4060(meta):
        return
    # The node object itself is gone (e.g. after a clean `kubectl delete
    # node` on laptop shutdown), not just NotReady -- same fallback either way.
    _set_agent_target(GTX1650_HOST, GTX1650_MODEL, logger)
