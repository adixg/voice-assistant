"""Retargets the `agent` Deployment's Ollama endpoint to whichever GPU node
is actually up. See README.md in this directory for the design and current
status -- this is real kopf/kubernetes-client code, but not yet validated
against a live cluster.

Run: kopf run controller.py --namespace=aicompanion
"""
import kopf
from kubernetes import client, config

NAMESPACE = "aicompanion"
AGENT_DEPLOYMENT = "agent"
GTX1650_HOST = "http://ollama-gtx1650:11434"
RTX4060_HOST = "http://ollama-rtx4060:11434"
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


def _set_agent_ollama_host(host: str, logger):
    """Patch the agent Deployment's env so it talks to `host`. Patching the
    pod template spec is itself what triggers a rollout -- Kubernetes does
    the restart, this just states the desired env value."""
    apps = client.AppsV1Api()
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "agent", "env": [{"name": "OLLAMA_HOST", "value": host}]}
                    ]
                }
            }
        }
    }
    apps.patch_namespaced_deployment(AGENT_DEPLOYMENT, NAMESPACE, patch)
    logger.info(f"agent OLLAMA_HOST -> {host}")


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
    _set_agent_ollama_host(RTX4060_HOST if ready else GTX1650_HOST, logger)


@kopf.on.delete("", "v1", "nodes")
def on_node_delete(meta, logger, **_):
    if not _is_rtx4060(meta):
        return
    # The node object itself is gone (e.g. after a clean `kubectl delete
    # node` on laptop shutdown), not just NotReady -- same fallback either way.
    _set_agent_ollama_host(GTX1650_HOST, logger)
