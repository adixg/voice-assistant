"""Small MCP stdio client used by the OpenAI-compatible agent backend.

The client intentionally supports only the MCP lifecycle and tools primitives
needed by the companion agent.  It launches a trusted local server without a
shell, discovers its schemas, and converts calls/results to OpenAI messages.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import threading
from typing import Any


class MCPClientError(RuntimeError):
    """The MCP server could not initialize or complete a request."""


class StdioMCPClient:
    def __init__(self, command: str, env: dict[str, str] | None = None):
        argv = shlex.split(command)
        if not argv:
            raise MCPClientError("MCP_SERVER_COMMAND is empty")
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
        )
        self._lock = threading.Lock()
        self._next_id = 1
        try:
            self._request("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "aicompanion-agent", "version": "0.1.0"},
            })
            self._notify("notifications/initialized", {})
            result = self._request("tools/list", {})
            self.tools = result.get("tools", []) if isinstance(result, dict) else []
        except Exception:
            self.close()
            raise

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise MCPClientError("MCP server stdin is closed")
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        if self.process.stdout is None:
            raise MCPClientError("MCP server stdout is closed")
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise MCPClientError("MCP server exited before replying")
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise MCPClientError(str(message["error"]))
            return message.get("result", {})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return self._read_response(request_id)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def openai_tools(self) -> list[dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
            },
        } for tool in self.tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", []) if isinstance(result, dict) else []
        text = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        return "\n".join(text) or json.dumps(result, sort_keys=True)

    def close(self) -> None:
        if getattr(self, "process", None) and self.process.poll() is None:
            self.process.terminate()

