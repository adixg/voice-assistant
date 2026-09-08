"""Concrete STT/LLM/TTS backends, discovered automatically.

Importing this package imports every module beside it, and each of those
registers its backend into voicepipe.registry as an import side effect. That
is the whole plug-and-play story: adding a backend means adding one file
here — no import to add, no list to extend, no entrypoint to edit.

    import voicepipe.backends   # everything below is now registered

Modules starting with "_" are skipped, so helpers can live here too.
"""
import importlib
import pkgutil


def load_all():
    """Import every backend module in this package. Idempotent."""
    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{__name__}.{info.name}")


load_all()
