#!/usr/bin/env python3
"""Live smoke test for model-generated MCP answers.

This intentionally checks the model-facing agent API rather than only calling
the MCP server directly. It is dependency-free and should be run inside the
cluster (or against a port-forward) with ``--agent-url`` and
``--prometheus-url`` pointing at reachable services.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PERCENT = re.compile(r"\b\d+(?:\.\d+)?%")


def get_json(url: str) -> dict:
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=15) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} returned a non-object JSON value")
    return payload


def ask(agent_url: str, prompt: str) -> str:
    body = json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode()
    request = Request(agent_url.rstrip("/") + "/ask", data=body,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=180) as response:
        payload = json.loads(response.read().decode())
    reply = payload.get("reply") if isinstance(payload, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError("agent returned no reply")
    return reply


def gpu_instances(prometheus_url: str) -> list[str]:
    query = urlencode({"query": "DCGM_FI_DEV_GPU_UTIL"})
    payload = get_json(prometheus_url.rstrip("/") + "/api/v1/query?" + query)
    result = payload.get("data", {}).get("result", [])
    return [item["metric"]["instance"] for item in result
            if isinstance(item, dict) and item.get("metric", {}).get("instance")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://agent:8002")
    parser.add_argument("--prometheus-url", default="http://prometheus:9090")
    parser.add_argument("--label", default="agent")
    args = parser.parse_args()

    instances = gpu_instances(args.prometheus_url)
    if not instances:
        raise RuntimeError("Prometheus returned no DCGM GPU instances")

    gpu_reply = ask(
        args.agent_url,
        "Use the get_gpu_status MCP tool and report the actual GPU and VRAM status. "
        "Include every returned host and numeric percentage. Do not guess.",
    )
    missing_hosts = [instance for instance in instances if instance not in gpu_reply]
    percentages = PERCENT.findall(gpu_reply)
    if missing_hosts:
        raise RuntimeError(f"{args.label}: GPU reply omitted live hosts: {missing_hosts}; reply={gpu_reply!r}")
    if len(percentages) < len(instances) * 2:
        raise RuntimeError(f"{args.label}: expected utilization and VRAM percentages; reply={gpu_reply!r}")

    health_reply = ask(
        args.agent_url,
        "Use the get_service_health MCP tool and summarize the current service health. "
        "Include gateway, stt, agent, and tts status. Do not guess.",
    )
    missing_services = [name for name in ("gateway", "stt", "agent", "tts")
                        if name not in health_reply.lower()]
    if missing_services:
        raise RuntimeError(f"{args.label}: service reply omitted {missing_services}; reply={health_reply!r}")

    print(f"PASS {args.label}: model produced GPU/VRAM and service-health MCP answers")
    print("GPU reply:", gpu_reply)
    print("Health reply:", health_reply)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - command-line failure path
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
