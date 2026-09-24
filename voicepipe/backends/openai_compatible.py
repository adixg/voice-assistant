"""A small client for an OpenAI-compatible chat-completions server.

This is deliberately named for the protocol, rather than one serving runtime.
It is the production backend for ``llama-server`` and can also be pointed at
vLLM, LM Studio, LiteLLM, or a hosted OpenAI-compatible endpoint.
"""
import json
import os

from ..registry import DELTA, FINAL, LLM, STATUS
from ..text import strip_think

DEFAULT_URL = "http://localhost:8080/v1"
DEFAULT_MODEL = "local-model"
DEFAULT_TIMEOUT = 120


# What the Stick shows while a tool runs (a STATUS event, forwarded by the
# gateway as "status:"). Without one, a tool turn looks like a frozen
# "thinking" screen for the several seconds the extra model round and the tool
# itself take. Short: the Stick's caption area is three lines.
TOOL_STATUS = {
    "get_weather": "checking the weather",
    "search_web": "searching the web",
    "get_time": "checking the time",
    "get_service_health": "checking the services",
    "get_gpu_status": "checking the GPUs",
    "get_agent_status": "checking the agent",
    "get_model_status": "checking the model",
    "get_stick_settings": "checking my settings",
    "set_stick_volume": "changing the volume",
    "set_stick_brightness": "changing the brightness",
}


def tool_status(name, arguments):
    """The progress line for one tool call, e.g. "checking the weather in Atlanta"."""
    text = TOOL_STATUS.get(name, f"using {name}")
    if name == "get_weather" and isinstance(arguments.get("location"), str):
        text += f" in {arguments['location'].split(',')[0].strip()}"
    elif name == "search_web" and isinstance(arguments.get("query"), str):
        text += f": {arguments['query'][:60]}"
    return text


class OpenAICompatibleUnavailable(RuntimeError):
    """The configured OpenAI-compatible endpoint cannot serve this backend."""


@LLM.register("openai-compatible")
class OpenAICompatibleLLM:
    """LLMBackend implemented against ``/v1/chat/completions``.

    ``client`` is injectable so protocol handling is unit-testable without a
    running server.  ``llama-server`` does not require an API key, but one is
    supported for compatible endpoints that do.
    """

    def __init__(self, url=DEFAULT_URL, model=DEFAULT_MODEL, key=None,
                 timeout=DEFAULT_TIMEOUT, client=None, mcp_client=None,
                 max_tool_rounds=4, thinking_switch=True):
        self.url = url.rstrip("/")
        self.model = model
        self.key = key or os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        self.timeout = timeout
        self._client = client
        self.mcp = mcp_client
        self.max_tool_rounds = max_tool_rounds
        self.thinking_switch = thinking_switch

    @staticmethod
    def add_arguments(group):
        group.add_argument("--openai-compatible-url",
                           default=os.environ.get("LLM_HOST", DEFAULT_URL),
                           help=f"OpenAI-compatible /v1 base URL (default: {DEFAULT_URL})")
        group.add_argument("--openai-compatible-model",
                           default=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
                           help=f"model alias exposed by that server (default: {DEFAULT_MODEL})")
        group.add_argument("--openai-compatible-key", default=None,
                           help="optional API key; defaults to $OPENAI_COMPATIBLE_API_KEY")
        group.add_argument("--openai-compatible-timeout", type=float,
                           default=DEFAULT_TIMEOUT,
                           help=f"turn timeout in seconds (default: {DEFAULT_TIMEOUT})")
        group.add_argument("--mcp-server-command", default=os.environ.get("MCP_SERVER_COMMAND"),
                           help="trusted MCP stdio server command (also via MCP_SERVER_COMMAND)")
        group.add_argument("--openai-compatible-no-thinking-switch", action="store_true",
                           default=os.environ.get("LLM_THINKING_SWITCH", "1") == "0",
                           help="don't send chat_template_kwargs.enable_thinking; use for strict "
                                "hosted endpoints that reject unknown fields (also LLM_THINKING_SWITCH=0)")

    @classmethod
    def from_args(cls, args):
        mcp = None
        command = getattr(args, "mcp_server_command", None) or os.environ.get("MCP_SERVER_COMMAND")
        if command:
            from ..mcp_stdio import StdioMCPClient
            mcp = StdioMCPClient(command)
        return cls(url=args.openai_compatible_url,
                   model=args.openai_compatible_model,
                   key=args.openai_compatible_key,
                   timeout=args.openai_compatible_timeout, mcp_client=mcp,
                   thinking_switch=not getattr(args, "openai_compatible_no_thinking_switch", False))

    @property
    def client(self):
        if self._client is None:
            import httpx
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            self._client = httpx.Client(base_url=self.url, headers=headers,
                                        timeout=self.timeout)
        return self._client

    def _request(self, messages, think, **extra):
        """The chat-completions body, carrying ``think`` when we can.

        OpenAI proper has no field for it, but llama.cpp and vLLM take
        ``chat_template_kwargs.enable_thinking`` for Qwen3-style templates.
        Without it Qwen3 reasons on every turn, and llama.cpp returns that
        reasoning in ``reasoning_content`` -- so ``strip_think`` never sees it
        and the caller just waits: measured 14.8 s vs 1.8 s per turn
        (benchmarks/pipeline/README.md). ``think=None`` means "server
        default", so nothing is sent.
        """
        request = {"model": self.model, "messages": messages, **extra}
        if self.thinking_switch and think is not None:
            request["chat_template_kwargs"] = {"enable_thinking": bool(think)}
        return request

    def ask(self, messages, think=None):
        for kind, text in self._tool_loop(messages, think):
            if kind == FINAL:
                return text
        return ""

    def _tool_loop(self, messages, think):
        """Run the MCP tool loop, yielding a STATUS before each tool call and one
        FINAL with the answer. Reasoning tags, if a model still emits them
        inline, are removed at the common text boundary as for Ollama."""
        try:
            working = list(messages)
            tools = self.mcp.openai_tools() if self.mcp else None
            for _ in range(self.max_tool_rounds + 1):
                request = self._request(working, think)
                if tools:
                    request["tools"] = tools
                response = self.client.post("/chat/completions", json=request)
                response.raise_for_status()
                message = response.json()["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []
                if not tool_calls or not self.mcp:
                    content = message.get("content") or ""
                    break
                working.append(message)
                for call in tool_calls:
                    function = call.get("function", {})
                    name = function.get("name")
                    arguments = json.loads(function.get("arguments") or "{}")
                    yield STATUS, tool_status(name, arguments)
                    result = self.mcp.call(name, arguments)
                    working.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
            else:
                raise OpenAICompatibleUnavailable("MCP tool loop exceeded max_tool_rounds")
        except Exception as e:  # noqa: BLE001 - normalize endpoint failures
            raise OpenAICompatibleUnavailable(
                f"OpenAI-compatible chat completion failed at {self.url}: {e}") from e
        yield FINAL, strip_think(content or "")

    def ask_stream(self, messages, think=None):
        # Tool calls require a complete response so the MCP loop can execute
        # them and replay the result, so MCP-enabled sessions don't stream the
        # reply's text; they do stream a STATUS per tool call ("checking the
        # weather in Atlanta"), then the answer as one FINAL. Ordinary no-tool
        # sessions keep token streaming below.
        if self.mcp is not None:
            yield from self._tool_loop(messages, think)
            return
        request = self._request(messages, think, stream=True)
        try:
            with self.client.stream("POST", "/chat/completions", json=request) as response:
                response.raise_for_status()
                parts = []
                for line in response.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                        content = payload["choices"][0].get("delta", {}).get("content")
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    if content:
                        parts.append(content)
                        yield DELTA, content
        except Exception as e:  # noqa: BLE001
            raise OpenAICompatibleUnavailable(
                f"OpenAI-compatible streaming completion failed at {self.url}: {e}") from e
        yield FINAL, strip_think("".join(parts))

    def check(self):
        try:
            response = self.client.get("/models", timeout=10)
            response.raise_for_status()
            names = [m["id"] for m in response.json().get("data", [])]
        except Exception as e:  # noqa: BLE001
            raise OpenAICompatibleUnavailable(
                f"can't reach an OpenAI-compatible server at {self.url}: {e}") from e
        print(f"  OpenAI-compatible @ {self.url} — {len(names)} models")
        if names and self.model not in names:
            return f"  ! '{self.model}' not offered. available: {', '.join(names[:8])}"
        return None

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
        if self.mcp is not None:
            self.mcp.close()
