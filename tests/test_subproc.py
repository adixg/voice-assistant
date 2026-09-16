"""voicepipe.subproc.WorkerVoice — the shared worker plumbing behind every
TTS backend, exercised against a fake worker script rather than a real model.

The fake speaks the same line protocol as tts_cli.py/chatterbox_cli.py, so
these cover the parts that used to be copy-pasted per backend: the "ready"
handshake, chunked synthesis, worker death mid-reply, and teardown.
"""
import os
import subprocess
import sys
import textwrap

import pytest

from voicepipe.subproc import WorkerVoice

# A stand-in for tts_cli.py --serve: prints "ready", then for each
# "<path>\t<text>" line writes a tiny wav and echoes the path back.
FAKE_WORKER = textwrap.dedent("""
    import sys, wave
    print("ready", flush=True)
    for line in sys.stdin:
        parts = line.rstrip("\\n").split("\\t")
        path = parts[0]
        if len(parts) > 1:
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b"\\x00\\x00" * 1600)
        print(path, flush=True)
""")

# Never prints "ready" — stands in for a worker whose model failed to load.
BROKEN_WORKER = 'import sys; sys.stderr.write("boom: no model\\n")'

# Answers the first request, then exits — a worker dying mid-reply.
DYING_WORKER = textwrap.dedent("""
    import sys
    print("ready", flush=True)
    line = sys.stdin.readline()
    print(line.rstrip("\\n").split("\\t")[0], flush=True)
    sys.exit(1)
""")


def make_voice(script, name="fake"):
    class FakeVoice(WorkerVoice):
        pass

    FakeVoice.name = name
    FakeVoice.command = lambda self: [sys.executable, "-c", script]
    return FakeVoice()


@pytest.fixture
def voice():
    v = make_voice(FAKE_WORKER)
    yield v
    v.close()


class TestStart:
    def test_waits_for_the_ready_handshake(self, voice):
        voice.start()
        assert voice.proc is not None and voice.proc.poll() is None

    def test_start_is_idempotent(self, voice):
        voice.start()
        first = voice.proc
        voice.start()
        assert voice.proc is first  # not respawned behind our back

    def test_a_worker_that_never_says_ready_raises_with_its_stderr(self):
        v = make_voice(BROKEN_WORKER)
        with pytest.raises(RuntimeError, match="failed to start") as excinfo:
            v.start()
        assert "boom: no model" in str(excinfo.value)  # the log tail is included

    def test_a_failed_start_leaves_nothing_running(self):
        v = make_voice(BROKEN_WORKER)
        with pytest.raises(RuntimeError):
            v.start()
        assert v.proc is None


class TestSynth:
    def test_returns_one_path_per_chunk_in_order(self, voice):
        paths = voice.synth("First sentence here. " * 30)

        assert len(paths) > 1
        assert paths == sorted(paths, key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]))
        assert all(os.path.exists(p) for p in paths)

    def test_starts_the_worker_on_demand(self, voice):
        assert voice.proc is None
        assert voice.synth("hello") != []
        assert voice.proc is not None

    def test_restarts_a_dead_worker(self, voice):
        voice.start()
        voice.proc.kill()
        voice.proc.wait()

        assert voice.synth("hello") != []  # transparently respawned

    def test_newlines_do_not_desynchronise_the_protocol(self, voice):
        """A newline in the reply would otherwise be read as a second request,
        leaving every later response off by one."""
        paths = voice.synth("first line\nsecond line")

        assert len(paths) == 1
        assert voice.synth("after") != []  # still in sync

    def test_a_worker_dying_mid_reply_returns_what_it_produced(self):
        v = make_voice(DYING_WORKER)
        try:
            paths = v.synth("Sentence one. " + "padding words " * 40 + ". Sentence two.")
            assert len(paths) >= 1  # partial result, not an exception
        finally:
            v.close()


class TestOutputIsolation:
    def test_two_backends_do_not_share_output_paths(self):
        """Fixed /tmp names meant a second backend (or a second bridge, or a
        benchmark beside a live one) overwrote the first one's audio."""
        a, b = make_voice(FAKE_WORKER, "a"), make_voice(FAKE_WORKER, "b")
        try:
            assert set(a.synth("hello")).isdisjoint(b.synth("hello"))
        finally:
            a.close()
            b.close()


class TestClose:
    def test_close_stops_the_worker_and_reaps_it(self, voice):
        voice.start()
        proc = voice.proc

        voice.close()

        assert proc.poll() is not None      # exited
        assert proc.returncode is not None  # and was waited for, so not a zombie
        assert voice.proc is None

    def test_close_removes_the_output_directory(self, voice):
        outdir = voice._outdir
        voice.synth("hello")
        assert os.path.isdir(outdir)

        voice.close()

        assert not os.path.exists(outdir)

    def test_close_is_safe_before_start_and_twice_over(self, voice):
        voice.close()
        voice.start()
        voice.close()
        voice.close()

    def test_restarting_does_not_leak_the_log_file_handle(self, voice):
        """start() reopens the worker's stderr log; without reaping the old
        handle each restart leaked a file descriptor."""
        voice.start()
        for _ in range(5):
            voice.proc.kill()
            voice.proc.wait()
            voice.start()

        open_logs = [f for f in os.listdir(f"/proc/{os.getpid()}/fd")
                     if _points_at(f, voice.logpath)]
        assert len(open_logs) == 1

    def test_context_manager_starts_and_closes(self):
        with make_voice(FAKE_WORKER) as v:
            assert v.proc is not None
            proc = v.proc
        assert proc.poll() is not None


def _points_at(fd, path):
    try:
        return os.readlink(f"/proc/{os.getpid()}/fd/{fd}") == path
    except OSError:
        return False


class TestCommandIsRequired:
    def test_the_base_class_refuses_to_start_without_one(self):
        with pytest.raises(NotImplementedError):
            WorkerVoice().start()


class TestRealBackendCommands:
    """The concrete backends' argv, without running them — the flags they
    build are otherwise only exercised by actually loading a model."""

    def test_chatterbox_passes_device_and_prompt(self):
        from voicepipe.backends.chatterbox import ChatterboxVoice

        cmd = ChatterboxVoice(device="cuda", prompt="/refs/hinata.wav").command()

        assert cmd[-4:] == ["--device", "cuda", "--prompt", "/refs/hinata.wav"]
        assert "--serve" in cmd

    def test_chatterbox_omits_prompt_for_the_default_voice(self):
        from voicepipe.backends.chatterbox import ChatterboxVoice

        assert "--prompt" not in ChatterboxVoice().command()

    def test_chatterbox_nano_is_opt_in(self):
        """Nano and Turbo load through the same class and differ only by this
        flag; Turbo stays the no-flag default so the argv is explicit."""
        from voicepipe.backends.chatterbox import ChatterboxVoice

        assert "--nano" not in ChatterboxVoice(nano=False).command()
        assert "--nano" in ChatterboxVoice(nano=True).command()

    def test_chatterbox_describes_which_model_it_loaded(self):
        """The startup line is the only place the running config is visible,
        so it has to name the variant, not just say 'chatterbox'."""
        from voicepipe.backends.chatterbox import ChatterboxVoice

        assert "Nano" in ChatterboxVoice(nano=True).describe()
        assert "Turbo" in ChatterboxVoice(nano=False).describe()

    def test_vits_passes_the_language_only_for_the_trilingual_model(self):
        from voicepipe.backends.vits import VitsVoice

        assert "-l" in VitsVoice(model="trilingual", lang="en").command()
        assert "-l" not in VitsVoice(model="japanese").command()

    def test_vits_speaker_is_stringified_for_argv(self):
        from voicepipe.backends.vits import VitsVoice

        cmd = VitsVoice(speaker=10).command()

        assert cmd[cmd.index("-s") + 1] == "10"


def test_workers_use_their_own_conda_env_python_when_it_exists():
    """On a dev machine, each engine's torch/CUDA pins conflict with the
    current interpreter's, so the worker must run under its own conda env's
    python instead -- but only when that env actually exists. A container
    image has no such conflict (it installs exactly one backend's deps
    directly into its own single interpreter), so there PYTHON must fall
    back to sys.executable rather than a hardcoded path that isn't there --
    see voicepipe/backends/vits.py's PYTHON comment for the reasoning."""
    from voicepipe.backends import chatterbox, vits

    for module in (chatterbox, vits):
        if os.path.exists(module._CONDA_PYTHON):
            assert module.PYTHON == module._CONDA_PYTHON
            assert "envs" in module.PYTHON
        else:
            assert module.PYTHON == sys.executable


def test_fake_worker_script_is_valid_python():
    """Guards the fixtures themselves: a syntax error in them would otherwise
    surface as a confusing 'worker failed to start'."""
    for script in (FAKE_WORKER, BROKEN_WORKER, DYING_WORKER):
        subprocess.run([sys.executable, "-c", f"compile({script!r}, 'x', 'exec')"], check=True)
