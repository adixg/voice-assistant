"""LLM turn-taking against Ollama, plus the built-in personas.

Standalone debug use:
    python -m voicepipe.llm --host http://localhost:11434 --model rina "hi there"
"""
from .registry import LLM

PARTNER_SYSTEM = (
    "You are Rina, the user's warm, playful, affectionate companion. "
    "Talk in casual, everyday language and keep replies to one or two sentences. "
    "Never narrate your own thoughts, never use stage directions, never use emojis. "
    "Stay in character and don't mention being an AI."
)

ASSISTANT_SYSTEM = (
    "You are a helpful, concise personal assistant. Answer directly in a neutral, "
    "friendly tone with no romantic or companion persona and no stage directions. "
    "Keep replies brief unless asked for more detail."
)

PERSONAS = {"partner": PARTNER_SYSTEM, "assistant": ASSISTANT_SYSTEM}
DEFAULT_PERSONA = "partner"

DEFAULT_SYSTEM = PARTNER_SYSTEM  # back-compat name; use PERSONAS/DEFAULT_PERSONA for new code


def strip_think(text):
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


def ask(client, model, messages, think):
    # think=False makes qwen3 & other reasoning models skip the <think> pass
    # (much faster); models that don't support the flag get a plain retry.
    kw = {} if think is None else {"think": think}
    try:
        resp = client.chat(model=model, messages=messages, **kw)
    except Exception as e:  # noqa: BLE001
        if kw and "think" in str(e).lower():
            resp = client.chat(model=model, messages=messages)
        else:
            raise
    return strip_think(resp["message"]["content"])


class OllamaLLM:
    """LLMBackend: chat against an Ollama server. Wraps ask() above with the
    client/model bound at construction, so callers only pass `messages`.

    `client` is normally left None (a real ollama.Client is built lazily from
    `host`); tests inject a fake one so importing this class doesn't require
    the `ollama` package or a reachable server.
    """

    def __init__(self, host="http://localhost:11434", model="rina", timeout=120, client=None):
        self.host = host
        self.model = model
        self.client = client if client is not None else self._make_client(host, timeout)

    @staticmethod
    def _make_client(host, timeout):
        import ollama
        return ollama.Client(host=host, timeout=timeout)

    def ask(self, messages, think=None):
        return ask(self.client, self.model, messages, think)

    def check(self):
        """Best-effort connectivity + model-availability check; prints
        status and exits the process on failure. Not part of LLMBackend —
        an optional extra callers may use for the "ollama" backend
        specifically, via `if hasattr(llm, "check"): llm.check()`."""
        import socket
        import sys
        from urllib.parse import urlparse
        u = urlparse(self.host)
        try:
            with socket.create_connection((u.hostname, u.port or 11434), timeout=4):
                pass
        except OSError as e:
            print(f"  ! can't reach {self.host}: {e}")
            sys.exit(1)
        try:
            names = [m.model for m in self.client.list().models]
        except Exception as e:  # noqa: BLE001
            print(f"  ! ollama on {self.host} answered but /api/tags failed: {e}")
            sys.exit(1)
        print(f"  ollama @ {self.host} — {len(names)} models")
        if self.model not in names and f"{self.model}:latest" not in names:
            print(f"  ! '{self.model}' not found. available: {', '.join(names) or '(none)'}")


LLM.register("ollama")(OllamaLLM)


if __name__ == "__main__":
    import argparse
    import ollama
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--model", default="rina")
    ap.add_argument("--persona", default=DEFAULT_PERSONA, choices=sorted(PERSONAS))
    ap.add_argument("--system", default=None, help="override --persona; pass '' to send none")
    ap.add_argument("--think", action="store_true")
    args = ap.parse_args()
    system = args.system if args.system is not None else PERSONAS[args.persona]
    client = ollama.Client(host=args.host, timeout=120)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": args.text})
    print(ask(client, args.model, messages, None if args.think else False))
