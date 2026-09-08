"""LLM turn-taking against an Ollama server.

Swapping the model is a flag (--model), and swapping Ollama itself for
something else is a new file next to this one — nothing here is assumed by
the entrypoints beyond the LLMBackend protocol.

Standalone debug use:
    python -m voicepipe ask "hi there" --model rina
"""
from ..registry import LLM
from ..text import strip_think

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "rina"
DEFAULT_PORT = 11434


class OllamaUnavailable(RuntimeError):
    """The server is unreachable or not answering as an Ollama server.

    Raised rather than exited on, so a caller embedding this backend (a test,
    a tool, a retry loop) decides what a failure means — only the entrypoints
    turn it into a process exit.
    """


@LLM.register("ollama")
class OllamaLLM:
    """LLMBackend: chat against an Ollama server.

    `client` is normally left None and built lazily from `host`; tests inject
    a fake so that importing this module needs neither the `ollama` package
    nor a reachable server.
    """

    def __init__(self, host=DEFAULT_HOST, model=DEFAULT_MODEL, timeout=120, client=None):
        self.host = host
        self.model = model
        self.client = client if client is not None else self._make_client(host, timeout)

    @staticmethod
    def _make_client(host, timeout):
        import ollama
        return ollama.Client(host=host, timeout=timeout)

    @classmethod
    def from_args(cls, args):
        return cls(host=args.host, model=args.model)

    def ask(self, messages, think=None):
        """One turn. `think=False` makes qwen3 and friends skip their <think>
        pass (much faster); models that reject the flag get a plain retry."""
        kwargs = {} if think is None else {"think": think}
        try:
            response = self.client.chat(model=self.model, messages=messages, **kwargs)
        except Exception as e:  # noqa: BLE001
            if kwargs and "think" in str(e).lower():
                response = self.client.chat(model=self.model, messages=messages)
            else:
                raise
        return strip_think(response["message"]["content"])

    def check(self):
        """Verify the server is reachable and report on the model.

        Returns a warning string if the server is fine but the model isn't
        loaded there, else None. Raises OllamaUnavailable if the server can't
        be used at all. Not part of LLMBackend — an optional extra the
        entrypoints call via `getattr(llm, "check", None)`.
        """
        import socket
        from urllib.parse import urlparse

        url = urlparse(self.host)
        try:
            with socket.create_connection((url.hostname, url.port or DEFAULT_PORT), timeout=4):
                pass
        except OSError as e:
            raise OllamaUnavailable(f"can't reach {self.host}: {e}") from e

        try:
            names = [m.model for m in self.client.list().models]
        except Exception as e:  # noqa: BLE001
            raise OllamaUnavailable(f"ollama on {self.host} answered but /api/tags failed: {e}") from e

        print(f"  ollama @ {self.host} — {len(names)} models")
        if self.model not in names and f"{self.model}:latest" not in names:
            return f"  ! '{self.model}' not found. available: {', '.join(names) or '(none)'}"
        return None
