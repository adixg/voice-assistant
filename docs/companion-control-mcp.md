# Companion-control MCP server

`tools/companion_control_mcp.py` is the project's first MCP server. It is a
small, dependency-free stdio JSON-RPC server intended to be launched by Hermes
Agent. Version 0.1 is intentionally **read-only**:

- `get_service_health` reads Prometheus scrape health, pod readiness, and
  container restarts.
- `get_gpu_status` reads DCGM GPU and VRAM utilization from Prometheus.
- `get_agent_status` reads the agent service's `/health` response.

It has no subprocess, filesystem, Kubernetes API, or device-control access.
That boundary is deliberate: a voice-triggered model must not receive generic
cluster or shell control. Future write tools will use a project-owned,
authenticated control API, typed bounds, device acknowledgement, explicit
confirmation in the voice flow, and an audit record.

## Hermes configuration

Hermes uses the Model Context Protocol over stdio. Add this to
`~/.hermes/config.yaml`, using the real repository path:

```yaml
mcp_servers:
  companion_control:
    command: /usr/bin/python3
    args: [/home/aditya/aigf/tools/companion_control_mcp.py]
    enabled: true
    trust: untrusted
    tools:
      include: [get_service_health, get_gpu_status, get_agent_status]
```

When Hermes runs on the host rather than in the Kubernetes namespace, expose
Prometheus locally first:

```bash
kubectl -n aicompanion port-forward svc/prometheus 9090:9090
```

Then start Hermes with:

```bash
export COMPANION_CONTROL_PROMETHEUS_URL=http://127.0.0.1:9090
export COMPANION_CONTROL_AGENT_URL=http://agent:8002
hermes chat
```

`get_service_health` and `get_gpu_status` need only Prometheus. For
`get_agent_status`, either run Hermes inside the namespace or port-forward the
agent and set `COMPANION_CONTROL_AGENT_URL=http://127.0.0.1:8002`.

## Manual protocol smoke test

This test does not need Hermes or a cluster:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python tools/companion_control_mcp.py
```

## llama.cpp and `--jinja`

The MCP server is separate from llama.cpp. `--jinja` switches llama.cpp to the
model's Jinja chat template, enabling its OpenAI-compatible endpoint to render
and parse structured function calls. It belongs on a llama.cpp canary only
after this MCP transport is proven; it does **not** grant the model any tool by
itself. Hermes owns MCP discovery and execution.
