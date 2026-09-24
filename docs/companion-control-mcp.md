# Companion-control MCP server

`tools/companion_control_mcp.py` is the project's first MCP server. It is a
small, dependency-free stdio JSON-RPC server launched by the agent container.
Version 0.1 was **read-only**; these tools still are:

- `get_service_health` reads Prometheus scrape health, pod readiness, and
  container restarts.
- `get_gpu_status` reads GPU and VRAM utilization, and any active throttling, from
  Prometheus (series from `services/gpu_exporter`).
- `get_agent_status` reads the agent service's `/health` response.
- `get_model_status` reads the configured llama.cpp route/model selected by the
  scheduler. It does not call llama.cpp recursively while a model turn waits
  for an MCP result.
- `get_time` returns the current date/time using the local IANA timezone
  database, defaulting to `America/New_York`.
- `search_web` queries the cluster-internal SearXNG service and returns source
  URLs and snippets.
- `get_weather` queries Open-Meteo for current conditions, hourly rain timing,
  and a seven-day daily forecast. It requires no API key for this
  non-commercial use.

**How a tool turn runs** (`voicepipe/backends/openai_compatible.py`): the agent
sends the chat plus the tool list to llama.cpp; if the reply asks for tools, it
runs each through this server, appends the results, and asks again, up to
`max_tool_rounds=4`. Before each call it streams a status line ("checking the
weather in Atlanta", "searching the web: ...") that the gateway forwards to the
Stick's caption, so a tool turn no longer looks frozen (2026-09-24). With tools
on, the reply text itself isn't streamed token by token; with one-sentence
replies that costs well under a second.

Since 0.2 (2026-09-24) it also has its first **write** tools, for the Stick:

- `get_stick_settings`: volume and brightness in percent, whether the screen
  is off, the firmware version.
- `set_stick_volume`, `set_stick_brightness`: exactly one of `percent`
  (absolute, 0-100) or `change` (relative, e.g. 15 for "a bit louder"),
  clamped to 0-100. Brightness 0 turns the screen off (a button tap shows it
  for 10 s). A volume above 75% carries a note that it can brown out the Stick
  on battery.

It still has no subprocess, filesystem or Kubernetes API access: a
voice-triggered model must not receive generic cluster or shell control.
The write tools follow the rules set for them before they existed:

- **A project-owned, authenticated control API**: the gateway's
  `GET/POST /device/settings`, off unless `GATEWAY_CONTROL_TOKEN` is set and
  then requiring it as a bearer token. The agent reads the same token from the
  `device-control` Secret (`COMPANION_CONTROL_TOKEN`), and never logs it.
- **Typed bounds**: the gateway accepts only integers 0-255 (422 otherwise).
- **Device acknowledgement**: the gateway sends `settings:V,B` down the relay's
  WebSocket, the phone turns it into a BLE `SETTINGS` frame, and the Stick's
  own report back (`settings:V,B,FIRMWARE`) is what the API returns. No
  confirmation within 5 s is a 504, and the tool reports it as an error.
- **An audit record**: every request is counted in
  `aicompanion_gateway_device_settings_changes_total{result=applied|timeout|no_stick}`
  and logged by the gateway with what was asked and what the Stick applied.
- **Explicit confirmation**: not asked for these two. Both are harmless and
  instantly reversible, so the spoken reply ("volume is at 40% now") is the
  confirmation. A future tool that isn't (e.g. switching the model) should ask.

Path of a change: agent (MCP tool) -> gateway `/device/settings` -> relay
WebSocket (app 1.4+) -> BLE `SETTINGS` -> Stick, and the Stick's report back the
same way. The gateway keeps reading the relay's socket while a turn runs, so a
change requested mid-reply is confirmed without waiting for the turn to end.

## Agent configuration

The Kubernetes agent launches this server over stdio:

```yaml
MCP_SERVER_COMMAND: python /app/tools/companion_control_mcp.py
COMPANION_CONTROL_PROMETHEUS_URL: http://prometheus:9090
COMPANION_CONTROL_AGENT_URL: http://agent:8002
COMPANION_CONTROL_SEARXNG_URL: http://searxng:8080
COMPANION_CONTROL_GATEWAY_URL: http://gateway:8000
COMPANION_CONTROL_TOKEN: (from the device-control Secret)
```

`get_service_health` and `get_gpu_status` need only Prometheus. The agent
health tool reads the in-cluster agent service. `get_model_status` uses the
`LLM_HOST` and `LLM_MODEL` environment inherited by the MCP subprocess.
`search_web` uses SearXNG;
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
