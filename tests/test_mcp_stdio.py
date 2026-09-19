"""Tests for the small MCP stdio transport client."""
import io
import json
import subprocess

import pytest

from voicepipe.mcp_stdio import MCPClientError, StdioMCPClient


class FakeProcess:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(("\n".join(lines) + "\n") if lines else "")
        self._returncode = None
        self.terminated = False

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15


def test_client_negotiates_discovers_tools_calls_and_closes(monkeypatch):
    process = FakeProcess([
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
            "tools": [{"name": "get_gpu_status", "description": "GPU", "inputSchema": {}}],
        }}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "result": {
            "content": [{"type": "text", "text": '{"utilization": 42}'}],
        }}),
    ])
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    client = StdioMCPClient("python companion_control_mcp.py", env={"PATH": "/bin"})
    assert client.openai_tools()[0]["function"]["name"] == "get_gpu_status"
    assert client.call("get_gpu_status", {}) == '{"utilization": 42}'
    client.close()

    sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert [message["method"] for message in sent] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    assert sent[-1]["params"] == {"name": "get_gpu_status", "arguments": {}}
    assert process.terminated is True


def test_empty_command_is_rejected():
    with pytest.raises(MCPClientError, match="empty"):
        StdioMCPClient("   ")


def test_malformed_response_terminates_the_process(monkeypatch):
    process = FakeProcess(["not-json"])
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(ValueError):
        StdioMCPClient("python server.py")
    assert process.terminated is True


def test_exited_server_is_reported(monkeypatch):
    process = FakeProcess([])
    process._returncode = 1
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(MCPClientError, match="exited before replying"):
        StdioMCPClient("python server.py")
