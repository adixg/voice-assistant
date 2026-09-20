"""voicepipe.registry — the name -> backend lookup, and the per-backend CLI
plumbing that keeps concrete backend names out of the entrypoints."""
import argparse

import pytest

from voicepipe.registry import Registry


class TestRegister:
    def test_create_dispatches_to_the_registered_factory_with_args_and_kwargs(self):
        reg = Registry("stt")
        calls = []

        @reg.register("fake")
        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            return "an instance"

        assert reg.create("fake", 1, 2, x=3) == "an instance"
        assert calls == [((1, 2), {"x": 3})]

    def test_register_returns_the_original_class_unchanged(self):
        """Registering must not wrap the class — it stays directly importable
        and constructible without going through the registry at all."""
        reg = Registry("tts")

        class Backend:
            pass

        assert reg.register("fake")(Backend) is Backend

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

    def test_empty_registry_points_at_the_likely_cause(self):
        reg = Registry("stt")
        with pytest.raises(ValueError, match=r"\(none registered"):
            reg.create("anything")


class TestAddArguments:
    """Each backend declares its own flags, so adding one needs no edit to
    any entrypoint's argument parser."""

    def test_each_backends_flags_are_added(self):
        reg = Registry("tts")

        @reg.register("noisy")
        class Noisy:
            @staticmethod
            def add_arguments(group):
                group.add_argument("--noisy-volume", type=int, default=11)

        ap = argparse.ArgumentParser()
        reg.add_arguments(ap)

        assert ap.parse_args([]).noisy_volume == 11
        assert ap.parse_args(["--noisy-volume", "3"]).noisy_volume == 3

    def test_backends_without_flags_are_skipped(self):
        reg = Registry("tts")
        reg.register("plain")(type("Plain", (), {}))

        ap = argparse.ArgumentParser()
        reg.add_arguments(ap)  # must not raise

        assert ap.parse_args([]) == argparse.Namespace()

    def test_several_backends_coexist_in_one_parser(self):
        reg = Registry("tts")

        @reg.register("a")
        class A:
            @staticmethod
            def add_arguments(group):
                group.add_argument("--a-device", default="cpu")

        @reg.register("b")
        class B:
            @staticmethod
            def add_arguments(group):
                group.add_argument("--b-device", default="cuda")

        ap = argparse.ArgumentParser()
        reg.add_arguments(ap)
        args = ap.parse_args([])

        assert (args.a_device, args.b_device) == ("cpu", "cuda")


class TestBuild:
    def test_build_uses_from_args_when_present(self):
        reg = Registry("tts")

        @reg.register("configured")
        class Configured:
            def __init__(self, device):
                self.device = device

            @classmethod
            def from_args(cls, args):
                return cls(device=args.device)

        built = reg.build("configured", argparse.Namespace(device="cuda"))

        assert isinstance(built, Configured) and built.device == "cuda"

    def test_build_falls_back_to_a_no_argument_constructor(self):
        reg = Registry("stt")

        @reg.register("simple")
        class Simple:
            pass

        assert isinstance(reg.build("simple", argparse.Namespace()), Simple)

    def test_build_reports_an_unknown_name_like_create_does(self):
        reg = Registry("llm")
        with pytest.raises(ValueError, match="unknown llm backend"):
            reg.build("nope", argparse.Namespace())


class TestBackendsAreAutoDiscovered:
    """Importing voicepipe.backends imports every module in the package, and
    each registers itself — this is what makes adding a backend a one-file
    change. Only registration is checked here; constructing them needs real
    models and separate conda envs."""

    def test_importing_the_package_registers_every_backend(self):
        import voicepipe.backends  # noqa: F401
        from voicepipe.registry import LLM, STT, TTS

        assert "faster-whisper" in STT.names()
        assert "ollama" in LLM.names()
        assert {"chatterbox", "kokoro", "vits"} <= set(TTS.names())

    def test_load_all_is_idempotent(self):
        from voicepipe.backends import load_all
        from voicepipe.registry import TTS

        before = TTS.names()
        load_all()

        assert TTS.names() == before

    def test_every_tts_backend_satisfies_the_protocol_shape(self):
        from voicepipe.registry import TTS, TTSBackend

        for name in TTS.names():
            backend = TTS.lookup(name)
            assert issubclass(backend, TTSBackend), name
