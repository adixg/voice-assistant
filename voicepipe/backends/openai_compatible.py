"""A small client for an OpenAI-compatible chat-completions server.

This is deliberately named for the protocol, rather than one serving runtime.
It is the production backend for ``llama-server`` and can also be pointed at
vLLM, LM Studio, LiteLLM, or a hosted OpenAI-compatible endpoint.
"""
import json
import os

from ..registry import DELTA, FINAL, LLM
from ..text import strip_think

DEFAULT_URL = "http://localhost:8080/v1"
DEFAULT_MODEL = "local-model"
DEFAULT_TIMEOUT = 120


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
                 timeout=DEFAULT_TIMEOUT, client=None):
        self.url = url.rstrip("/")
        self.model = model
        self.key = key or os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        self.timeout = timeout
        self._client = client

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

    @classmethod
    def from_args(cls, args):
        return cls(url=args.openai_compatible_url,
                   model=args.openai_compatible_model,
                   key=args.openai_compatible_key,
                   timeout=args.openai_compatible_timeout)

    @property
    def client(self):
        if self._client is None:
            import httpx
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            self._client = httpx.Client(base_url=self.url, headers=headers,
                                        timeout=self.timeout)
        return self._client

    def ask(self, messages, think=None):
        # ``think`` is a vendor extension: OpenAI-compatible servers have no
        # portable field for it.  Reasoning tags, if a model emits them, are
        # removed at the common text boundary just as they are for Ollama.
        try:
            response = self.client.post("/chat/completions", json={
                "model": self.model,
                "messages": messages,
            })
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001 - normalize endpoint failures
            raise OpenAICompatibleUnavailable(
                f"OpenAI-compatible chat completion failed at {self.url}: {e}") from e
        return strip_think(content or "")

    def ask_stream(self, messages, think=None):
        request = {"model": self.model, "messages": messages, "stream": True}
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
