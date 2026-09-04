"""voicepipe.registry — the name -> factory lookup that lets chat_loop.py and
bridge_server.py pick an STT/LLM/TTS backend by name instead of importing one
concrete backend directly."""
import pytest

from voicepipe.registry import Registry


class TestRegistry:
    def test_create_dispatches_to_the_registered_factory_with_args_and_kwargs(self):
        reg = Registry("stt")
        calls = []

        @reg.register("fake")
        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            return "an instance"

        result = reg.create("fake", 1, 2, x=3)

        assert result == "an instance"
        assert calls == [((1, 2), {"x": 3})]

    def test_register_returns_the_original_factory_unchanged(self):
        reg = Registry("tts")

        def factory():
            return object()

        assert reg.register("fake")(factory) is factory

    def test_unknown_name_raises_with_the_kind_and_available_names(self):
        reg = Registry("llm")
        reg.register("known")(lambda: None)

        with pytest.raises(ValueError, match="llm backend 'missing'.*known"):
            reg.create("missing")

    def test_names_are_sorted(self):
        reg = Registry("tts")
        reg.register("zeta")(lambda: None)
        reg.register("alpha")(lambda: None)

        assert reg.names() == ["alpha", "zeta"]

    def test_empty_registry_reports_none_registered(self):
        reg = Registry("stt")
        with pytest.raises(ValueError, match=r"\(none registered"):
            reg.create("anything")


class TestDefaultBackendsAreRegistered:
    """Importing the concrete backend modules registers them into the shared
    STT/LLM/TTS registries as a side effect — this is what lets
    chat_loop.py/bridge_server.py list valid --*-backend choices without a
    separate plugin-discovery step. Only presence is checked here; actually
    constructing faster-whisper/VITS backends needs a real model (see
    test_make_face_sprites.py's neighbours for what's deliberately untested)."""

    def test_faster_whisper_stt_is_registered(self):
        import voicepipe.stt  # noqa: F401
        from voicepipe.registry import STT
        assert "faster-whisper" in STT.names()

    def test_ollama_llm_is_registered(self):
        import voicepipe.llm  # noqa: F401
        from voicepipe.registry import LLM
        assert "ollama" in LLM.names()

    def test_vits_tts_is_registered(self):
        import voicepipe.tts  # noqa: F401
        from voicepipe.registry import TTS
        assert "vits" in TTS.names()
