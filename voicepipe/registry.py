"""Backend interfaces + a name -> factory registry, so chat_loop.py and
bridge_server.py can pick an STT/LLM/TTS implementation by name (a CLI flag,
in practice) instead of importing one concrete backend directly.

Swapping in a new engine — a different STT model, a cloud LLM, a different
TTS voice, an agentic Hermes/Qwen setup — means writing a class that matches
the relevant Protocol below and registering it:

    from voicepipe.registry import LLM

    class MyLLM:
        def ask(self, messages, think=None):
            ...

    LLM.register("my-llm")(MyLLM)

Nothing in chat_loop.py/bridge_server.py has to change beyond passing
"my-llm" as --llm-backend.

The Protocols are structural (PEP 544): a backend class doesn't need to
inherit from anything, it just needs the matching method(s). Importing a
backend module (voicepipe.stt, .llm, .tts) registers its backend(s) as a
side effect of that module executing — the entrypoints already import those
modules for their other helpers (ensure_cuda_libs, DEFAULT_SYSTEM, ...), so
no separate plugin-discovery step is needed; a backend that lives elsewhere
just needs importing once before its name is looked up.
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class STTBackend(Protocol):
    """transcribe(wav_path, lang) -> text. `lang` is a language code, or None
    to auto-detect."""

    def transcribe(self, wav_path: str, lang) -> str: ...


@runtime_checkable
class LLMBackend(Protocol):
    """ask(messages, think) -> reply text. `messages` is an OpenAI/Ollama-
    style list of {"role", "content"} dicts (including the system prompt, if
    any — backends don't manage conversation history themselves). `think` is
    True/False/None, where None means don't pass the flag at all, for models
    that don't support it."""

    def ask(self, messages: list, think=None) -> str: ...


@runtime_checkable
class TTSBackend(Protocol):
    """synth(text) -> list of wav file paths, one per chunk. close() releases
    any worker process/model the backend is holding open."""

    def synth(self, text: str) -> list: ...
    def close(self) -> None: ...


class Registry:
    """Name -> factory map for one backend kind (stt/llm/tts). `create()`
    just calls the registered factory with whatever args/kwargs it's given —
    different backends legitimately need different construction parameters,
    only the resulting instance's public interface has to match the
    Protocol above."""

    def __init__(self, kind):
        self.kind = kind
        self._factories = {}

    def register(self, name):
        def deco(factory):
            self._factories[name] = factory
            return factory
        return deco

    def create(self, name, *args, **kwargs):
        try:
            factory = self._factories[name]
        except KeyError:
            available = ", ".join(sorted(self._factories)) or "(none registered — is the backend module imported?)"
            raise ValueError(f"unknown {self.kind} backend {name!r}; available: {available}") from None
        return factory(*args, **kwargs)

    def names(self):
        return sorted(self._factories)


STT = Registry("stt")
LLM = Registry("llm")
TTS = Registry("tts")
