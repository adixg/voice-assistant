"""Backend interfaces + a name -> class registry, so the entrypoints pick an
STT/LLM/TTS implementation by name (a CLI flag, in practice) instead of
importing a concrete one.

Adding a backend is meant to be *one file*. Drop it in voicepipe/backends/
and it is discovered, registered, listed in `--tts-backend --help`, and
constructible — with no edit to chat_loop.py, bridge_server.py, or anything
else:

    from voicepipe.registry import LLM

    @LLM.register("my-llm")
    class MyLLM:
        @staticmethod
        def add_arguments(group):          # optional: your own CLI flags
            group.add_argument("--my-llm-endpoint", default="http://localhost:9000")

        @classmethod
        def from_args(cls, args):          # optional: build from those flags
            return cls(endpoint=args.my_llm_endpoint)

        def __init__(self, endpoint):
            self.endpoint = endpoint

        def ask(self, messages, think=None):
            ...

The Protocols below are structural (PEP 544): a backend doesn't inherit from
anything, it just needs the matching methods. `add_arguments`/`from_args` are
both optional — a backend that needs no configuration is simply constructed
with no arguments.

Backends take ordinary keyword arguments, never an argparse namespace, so
they stay usable from plain Python (a notebook, a test, another tool);
`from_args` is only the adapter between the two.
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class STTBackend(Protocol):
    """transcribe(wav_path, lang) -> text. `lang` is a language code, or None
    to auto-detect."""

    def transcribe(self, wav_path: str, lang) -> str: ...


# ---------------------------------------------------------------- reply events
# What a streaming backend yields. A chat model answers in about a second, so
# one string is a fine answer shape for it; an agent runs tools for tens of
# seconds, and a caller with no way to show progress leaves the user staring at
# a frozen screen wondering whether it crashed. These three let a backend say
# what it is doing while it does it.
STATUS = "status"  # transient progress, e.g. "searching the web" — display, don't keep
DELTA = "delta"    # an incremental piece of the reply text
FINAL = "final"    # the complete reply; emitted exactly once, last
# The tool calls a turn made and their (trimmed) results, as a JSON list of
# OpenAI-style chat messages, emitted just before FINAL by a backend that ran
# tools. Callers keeping a conversation should store them ahead of the reply:
# a history holding only reply *text* ("I dimmed the screen") teaches the model
# that saying so is enough, and it stops calling the tool (measured 3/3 on
# qwen3-8b, 2026-09-24). Callers that don't keep history can ignore it.
TOOLS = "tools"


@runtime_checkable
class LLMBackend(Protocol):
    """ask(messages, think) -> reply text. `messages` is an OpenAI/Ollama-style
    list of {"role", "content"} dicts (including the system prompt, if any —
    backends don't manage conversation history themselves). `think` is
    True/False/None, where None means don't pass the flag at all, for models
    that don't support it.

    A backend MAY also implement:

        ask_stream(messages, think) -> Iterator[tuple[str, str]]

    yielding (STATUS|DELTA|FINAL, text) as the answer is produced, ending with
    exactly one FINAL carrying the whole reply. It is optional on purpose:
    `ask()` stays the contract every backend must meet, and callers use
    `stream_reply()` below, which falls back to `ask()` when a backend has no
    streaming to offer.
    """

    def ask(self, messages: list, think=None) -> str: ...


def stream_reply(llm, messages, think=None):
    """Yield (kind, text) events from `llm`, whether or not it can stream.

    This is what callers should use, so that adding a streaming backend needs
    no change at the call site and a non-streaming one needs no shim: a
    backend without `ask_stream` simply produces a single FINAL event.
    """
    ask_stream = getattr(llm, "ask_stream", None)
    if ask_stream is None:
        yield FINAL, llm.ask(messages, think)
        return
    try:
        yield from ask_stream(messages, think)
    except NotImplementedError:
        # Declared but not built yet — treat exactly like having no streaming.
        yield FINAL, llm.ask(messages, think)


@runtime_checkable
class SpeakerBackend(Protocol):
    """embed(wav_path) -> a unit-length vector identifying who is speaking.

    Unit length is part of the contract: it makes a dot product the cosine
    similarity, so comparing voices needs no normalisation at the call site.

    A backend also declares the numbers that are properties of *its* model,
    not of speaker verification in general — different models put their
    same-speaker and different-speaker clusters in different places, and need
    different amounts of audio before a score means anything:

        name              str   identifies embeddings this produced
        dimensions        int   length of the vector
        default_threshold float cosine score above which it's the same person
        min_verify_seconds float below this, don't judge at all

    Those exist so that adding a second backend is one file, rather than one
    file plus a set of constants edited elsewhere to match it.
    """

    name: str
    dimensions: int
    default_threshold: float
    min_verify_seconds: float

    def embed(self, wav_path: str): ...


@runtime_checkable
class TTSBackend(Protocol):
    """synth(text) -> list of wav file paths, one per chunk. close() releases
    any worker process or model the backend is holding open."""

    def synth(self, text: str) -> list: ...
    def close(self) -> None: ...


class Registry:
    """Name -> backend class map for one backend kind (stt/llm/tts)."""

    def __init__(self, kind):
        self.kind = kind
        self._factories = {}

    def register(self, name):
        """Decorator: register a backend class (or any factory) under `name`.

        Returns the class unchanged, so it stays directly importable and
        usable — registering doesn't wrap or alter it.
        """
        def deco(factory):
            self._factories[name] = factory
            return factory
        return deco

    def names(self):
        return sorted(self._factories)

    def lookup(self, name):
        try:
            return self._factories[name]
        except KeyError:
            available = ", ".join(self.names()) or "(none registered — is voicepipe.backends imported?)"
            raise ValueError(f"unknown {self.kind} backend {name!r}; available: {available}") from None

    def create(self, name, *args, **kwargs):
        """Construct a backend directly, passing arguments through untouched."""
        return self.lookup(name)(*args, **kwargs)

    def add_arguments(self, parser):
        """Give every registered backend a chance to declare its own CLI flags.

        Each backend's flags land in their own `--help` group, so the
        entrypoints never mention a specific backend's options.
        """
        for name in self.names():
            factory = self._factories[name]
            adder = getattr(factory, "add_arguments", None)
            if adder is None:
                continue
            adder(parser.add_argument_group(f"{self.kind} backend '{name}'"))

    def options(self, name, args):
        """The parsed values of the flags backend `name` declares, e.g.
        {"kitten_voice": "Bella", "kitten_speed": 1.6}, so a service can say
        exactly how its backend is configured (defaults included) without
        listing every other backend's flags too."""
        import argparse

        adder = getattr(self.lookup(name), "add_arguments", None)
        if adder is None:
            return {}
        probe = argparse.ArgumentParser(add_help=False)
        adder(probe)
        return {a.dest: getattr(args, a.dest, None) for a in probe._actions}

    def build(self, name, args):
        """Construct the named backend from parsed CLI args, via its
        `from_args` adapter (or with no arguments if it doesn't define one)."""
        factory = self.lookup(name)
        from_args = getattr(factory, "from_args", None)
        return from_args(args) if from_args else factory()


STT = Registry("stt")
LLM = Registry("llm")
TTS = Registry("tts")
SV = Registry("speaker")
