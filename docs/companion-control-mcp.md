# Companion-control MCP server

`tools/companion_control_mcp.py` is the project's first MCP server. It is a
small, dependency-free stdio JSON-RPC server launched by the agent container.
Version 0.1 is intentionally **read-only**:

- `get_service_health` reads Prometheus scrape health, pod readiness, and
  container restarts.
- `get_gpu_status` reads DCGM GPU and VRAM utilization from Prometheus.
- `get_agent_status` reads the agent service's `/health` response.
- `get_model_status` reads the configured llama.cpp route/model and verifies
  the model exposed by that route's `/v1/models` endpoint.
- `get_time` returns the current date/time using the local IANA timezone
  database, defaulting to `America/New_York`.
- `search_web` queries the cluster-internal SearXNG service and returns source
  URLs and snippets.
- `get_weather` queries Open-Meteo for current conditions, hourly rain timing,
  and a seven-day daily forecast. It requires no API key for this
  non-commercial use.

It has no subprocess, filesystem, Kubernetes API, or device-control access.
That boundary is deliberate: a voice-triggered model must not receive generic
cluster or shell control. Future write tools will use a project-owned,
authenticated control API, typed bounds, device acknowledgement, explicit
confirmation in the voice flow, and an audit record.

## Agent configuration

The Kubernetes agent launches this server over stdio:

```yaml
MCP_SERVER_COMMAND: python /app/tools/companion_control_mcp.py
COMPANION_CONTROL_PROMETHEUS_URL: http://prometheus:9090
COMPANION_CONTROL_AGENT_URL: http://agent:8002
COMPANION_CONTROL_SEARXNG_URL: http://searxng:8080
```

`get_service_health` and `get_gpu_status` need only Prometheus. The agent
health tool reads the in-cluster agent service. `get_model_status` uses the
`LLM_HOST` and `LLM_MODEL` environment inherited by the MCP subprocess and
queries the active llama.cpp `/v1/models` endpoint. `search_web` uses SearXNG;
`get_weather` uses Open-Meteo directly over HTTPS. Neither requires a secret.

## Manual protocol smoke test

This test does not need a cluster:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python tools/companion_control_mcp.py
```

## Qwen3 + llama.cpp agent path

The CPU-only `services/agent` container can now own the small orchestration
loop instead of requiring a separate agent runtime. Set `MCP_SERVER_COMMAND` to a trusted
stdio server command. The backend discovers `tools/list`, sends the resulting
OpenAI tool schemas to llama.cpp, executes returned `tool_calls` through
`tools/call`, and replays results until a final answer (up to four rounds).

The Kubernetes deployment wires the read-only companion server automatically:

```yaml
MCP_SERVER_COMMAND: python /app/tools/companion_control_mcp.py
COMPANION_CONTROL_PROMETHEUS_URL: http://prometheus:9090
COMPANION_CONTROL_AGENT_URL: http://agent:8002
COMPANION_CONTROL_SEARXNG_URL: http://searxng:8080
```

`--jinja` remains a llama.cpp setting: it enables structured function calls,
but it does not execute tools. The agent container is the orchestrator and MCP
server is the permission boundary.
