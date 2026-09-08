"""Hermes Agent (Nous Research) — a full agent behind the LLM slot.

Unlike the `ollama` backend, which talks to a bare model, this talks to a
running agent: Hermes Agent answers `/v1/chat/completions` with its whole
toolset engaged — terminal, files, web search, persistent memory, and the
skills it has written for itself — and returns the final reply. From this
project's side it is still just `ask(messages) -> str`, so the bridge, the
wire protocol and the firmware are all unchanged.

Start the agent first (see its docs), with `~/.hermes/.env` containing:

    API_SERVER_ENABLED=true
    API_SERVER_KEY=<a long random string>

then `hermes gateway`, which listens on http://127.0.0.1:8642.

The endpoint is plain OpenAI-compatible chat completions, so the same backend
drives anything else that speaks it — OpenRouter, vLLM, llama.cpp's server,
LM Studio, LiteLLM — by pointing --hermes-url elsewhere.

Two things worth knowing before wiring this to a voice device:

* An agent turn is not a chat turn. Tool calls can take tens of seconds,
  which is why the timeout below defaults far higher than a chat model needs.
  `ask_stream()` consumes Hermes' progress events so the caller can show what
  the agent is doing instead of leaving the screen frozen; `ask()` remains for
  callers that just want the finished string.
* Hermes treats the API server as an *unattended* surface — no human is there
  to approve a dangerous command — so its `unattended_mode` defaults to
  `deny`. Leave it there unless you have thought hard about a voice-triggered
  agent auto-approving shell commands.
"""
import json
import os

from ..registry import DELTA, FINAL, LLM, STATUS
from ..text import strip_think

DEFAULT_URL = "http://127.0.0.1:8642/v1"
DEFAULT_MODEL = "hermes-agent"
# Agent turns run tools; a chat-length timeout would abort real work midway.
DEFAULT_TIMEOUT = 180
KEY_ENV_VARS = ("HERMES_API_KEY", "API_SERVER_KEY")


class HermesAgentUnavailable(RuntimeError):
    """The agent isn't reachable, or rejected our credentials.

    Raised rather than exited on, so callers decide what a failure means —
    only the entrypoints turn it into a process exit.
    """


def _key_from_env():
    for name in KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


# ------------------------------------------------------- SSE frame inspection
# Kept as free functions so they can be tested against captured frames without
# a client, a server, or a network.

# Hermes names its own progress frames; a plain OpenAI server sends none.
_PROGRESS_HINTS = ("tool", "progress")

# What the agent calls its own thinking step, which reads badly as a tool name.
_THINKING_NAMES = {"_thinking", "thinking"}


def _is_progress_event(event_name, payload):
    """True for a tool-progress frame rather than a reply chunk."""
    if event_name and any(hint in event_name.lower() for hint in _PROGRESS_HINTS):
        return True
    # Some servers omit the `event:` line and mark the frame in the body.
    kind = payload.get("object") or payload.get("type") if isinstance(payload, dict) else None
    return bool(kind and any(hint in str(kind).lower() for hint in _PROGRESS_HINTS))


def _progress_label(payload):
    """A short human label for a progress frame, or None if there's nothing
    worth showing. The `/v1/runs` shape carries `tool_name` and `delta`."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("tool_name") or payload.get("name")
    if isinstance(name, str) and name.strip():
        name = name.strip()
        return "thinking" if name in _THINKING_NAMES else name.replace("_", " ")
    for key in ("message", "status", "delta"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _delta_text(payload):
    """The incremental reply text in an OpenAI `chat.completion.chunk`, if any."""
    if not isinstance(payload, dict):
        return None
    try:
        choice = payload["choices"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(choice, dict):
        return None
    delta = choice.get("delta") or {}
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) and content else None


@LLM.register("hermes-agent")
class HermesAgentLLM:
    """LLMBackend: an OpenAI-compatible chat-completions client.

    `client` is normally left None and built lazily; tests inject a fake so
    this module imports and exercises without a running agent.
    """

    def __init__(self, url=DEFAULT_URL, key=None, model=DEFAULT_MODEL,
                 timeout=DEFAULT_TIMEOUT, client=None):
        self.url = url.rstrip("/")
        self.key = key or _key_from_env()
        self.model = model
        self.timeout = timeout
        self._client = client

    @staticmethod
    def add_arguments(group):
        group.add_argument("--hermes-url", default=DEFAULT_URL,
                           help=f"OpenAI-compatible base URL (default: {DEFAULT_URL}); also "
                                f"works for OpenRouter, vLLM, llama.cpp, LM Studio, LiteLLM")
        group.add_argument("--hermes-key", default=None,
                           help=f"API key; falls back to ${' or $'.join(KEY_ENV_VARS)}")
        group.add_argument("--hermes-model", default=DEFAULT_MODEL,
                           help=f"model name to request (default: {DEFAULT_MODEL})")
        group.add_argument("--hermes-timeout", type=float, default=DEFAULT_TIMEOUT,
                           help=f"seconds to wait for a turn — agent turns run tools and are "
                                f"far slower than chat (default: {DEFAULT_TIMEOUT})")

    @classmethod
    def from_args(cls, args):
        return cls(url=args.hermes_url, key=args.hermes_key,
                   model=args.hermes_model, timeout=args.hermes_timeout)

    @property
    def client(self):
        """Built lazily so importing this module needs no network and no httpx
        connection until something actually asks."""
        if self._client is None:
            import httpx
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            self._client = httpx.Client(base_url=self.url, headers=headers, timeout=self.timeout)
        return self._client

    def ask(self, messages, think=None):
        """One agent turn. `think` is ignored: the agent decides its own
        reasoning depth, and there is no OpenAI-compatible field for it."""
        response = self.client.post("/chat/completions", json={
            "model": self.model,
            "messages": messages,
        })
        response.raise_for_status()
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise HermesAgentUnavailable(
                f"unexpected response shape from {self.url}: {str(payload)[:200]}") from e
        return strip_think(content or "")

    def ask_stream(self, messages, think=None):
        """One agent turn, reported as it happens.

        Yields (STATUS, label) while the agent runs a tool, (DELTA, text) for
        each chunk of the answer, and exactly one (FINAL, whole_reply) at the
        end — which is what lets the Stick show "searching the web" instead of
        sitting frozen through a 25-second tool call.

        The parser is deliberately loose about event shapes. Hermes' docs
        promise `event: hermes.tool.progress` on this endpoint, but that
        emission isn't greppable in `gateway/platforms/api_server.py` (only the
        `/v1/runs` one, which carries `tool_name` and `delta`), so rather than
        hard-code a shape that might be wrong, anything event-named like tool
        progress becomes a STATUS and anything unrecognised is skipped. A
        server that sends only plain OpenAI chunks still streams correctly.
        """
        request = {"model": self.model, "messages": messages, "stream": True}
        with self.client.stream("POST", "/chat/completions", json=request) as response:
            response.raise_for_status()
            parts = []
            event_name = None
            for line in response.iter_lines():
                line = line.strip()
                if not line:
                    event_name = None  # blank line ends an SSE frame
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except ValueError:
                    continue

                if _is_progress_event(event_name, payload):
                    label = _progress_label(payload)
                    if label:
                        yield STATUS, label
                    continue

                chunk = _delta_text(payload)
                if chunk:
                    parts.append(chunk)
                    yield DELTA, chunk

        yield FINAL, strip_think("".join(parts))

    def check(self):
        """Verify the agent is up and the key works.

        Returns a warning string if it answers but doesn't list the model we
        intend to request, else None. Raises HermesAgentUnavailable if it
        can't be used at all.
        """
        import httpx

        try:
            response = self.client.get("/models", timeout=10)
        except httpx.HTTPError as e:
            raise HermesAgentUnavailable(
                f"can't reach the Hermes Agent API at {self.url}: {e}. Is `hermes gateway` "
                f"running with API_SERVER_ENABLED=true?") from e

        if response.status_code in (401, 403):
            raise HermesAgentUnavailable(
                f"{self.url} rejected the API key (HTTP {response.status_code}); "
                f"set --hermes-key or ${KEY_ENV_VARS[0]} to match API_SERVER_KEY")
        try:
            response.raise_for_status()
            names = [m["id"] for m in response.json().get("data", [])]
        except Exception as e:  # noqa: BLE001
            raise HermesAgentUnavailable(f"{self.url}/models answered but wasn't usable: {e}") from e

        print(f"  hermes-agent @ {self.url} — {len(names)} models")
        if names and self.model not in names:
            return f"  ! '{self.model}' not offered. available: {', '.join(names[:8]) or '(none)'}"
        return None

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
