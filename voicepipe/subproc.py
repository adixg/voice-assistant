"""Shared machinery for TTS backends that run their model in a separate
process.

Every heavy TTS engine here lives in its own conda env (incompatible torch and
CUDA pins), so the `chat` env can't import them — it talks to a long-lived
worker over stdin/stdout instead, which also means the model loads once
rather than per reply. That plumbing (spawn, handshake, chunked request/
response, teardown) is identical for every engine, so it lives here and a
concrete backend only has to say what command to run.

The worker protocol, one request per line, tab separated:

    <outpath>\\t<text>     synthesize into that path
    <outpath>             no-op ping, echoed back

The worker replies with the outpath on success or "ERR\\t<message>" on
failure, and prints exactly "ready" on stdout once its model is loaded.
"""
import os
import shutil
import subprocess
import tempfile

from .text import chunks, one_line

READY = "ready"
STOP_TIMEOUT = 5  # seconds to wait for a worker to exit before killing it


class WorkerVoice:
    """A TTS backend backed by a long-lived subprocess.

    Subclasses set `name` and implement `command()`. Everything else —
    starting the worker, synthesizing, cleaning up — is handled here.

    synth() returns wav paths and plays nothing, so the same code path serves
    chat_loop.py (which plays them locally) and bridge_server.py (which
    streams them to the Stick).
    """

    name = "tts"

    def __init__(self, hooks=None):
        self.hooks = hooks
        self.proc = None
        self._log = None
        self.logpath = os.path.join(tempfile.gettempdir(), f"{self.name}_worker.log")
        # Per-instance output directory. Two backends (or two bridges, or a
        # benchmark running beside a live bridge) would otherwise write to the
        # same fixed /tmp names and overwrite each other's audio mid-reply.
        self._outdir = tempfile.mkdtemp(prefix=f"voicepipe-{self.name}-")

    def command(self):
        """The argv for the worker process. Implemented by each backend."""
        raise NotImplementedError

    def describe(self):
        """One line about this backend's configuration, for the startup log."""
        return self.name

    # ------------------------------------------------------------- lifecycle
    def start(self):
        """Spawn the worker and block until its model is loaded.

        Called eagerly by the entrypoints so the model-load cost is paid at
        startup rather than on the first reply.
        """
        if self.proc is not None and self.proc.poll() is None:
            return
        self._reap()
        print(f"  starting {self.describe()} ...", flush=True)
        self._log = open(self.logpath, "w")
        self.proc = subprocess.Popen(
            self.command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
            text=True, bufsize=1,
        )
        line = self.proc.stdout.readline().strip()
        if line != READY:
            tail = self._log_tail()
            self.close()
            raise RuntimeError(f"{self.name} worker failed to start ({line!r})\n{tail}")

    def _log_tail(self, limit=800):
        try:
            with open(self.logpath) as f:
                return f.read()[-limit:]
        except OSError:
            return ""

    def _reap(self):
        """Release a previous worker's process and log handle.

        Without this, a worker that dies and gets restarted mid-run leaks a
        file descriptor each time and leaves the dead process unwaited-for.
        """
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
            self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def close(self):
        """Stop the worker and remove this instance's audio directory.

        Safe to call more than once, and safe to call on a backend that was
        never started.
        """
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # EOF ends the worker's stdin loop
            except OSError:
                pass
            try:
                self.proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self._reap()
        shutil.rmtree(self._outdir, ignore_errors=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- synthesis
    def synth(self, text):
        """Synthesize `text`, returning one wav path per chunk, in order.

        Restarts the worker if it has died. On a mid-reply failure the chunks
        already produced are returned rather than raising, so the caller can
        still speak what it has.
        """
        if self.proc is None or self.proc.poll() is not None:
            self.start()

        paths = []
        for i, chunk in enumerate(chunks(text)):
            wav = os.path.join(self._outdir, f"chunk_{i}.wav")
            try:
                self.proc.stdin.write(f"{wav}\t{one_line(chunk)}\n")
                self.proc.stdin.flush()
                response = self.proc.stdout.readline().strip()
            except (BrokenPipeError, OSError) as e:
                print(f"  ({self.name}: worker gone: {e})", flush=True)
                break
            if response != wav:
                detail = response or f"worker died, see {self.logpath}"
                print(f"  ({self.name}: {detail})", flush=True)
                break
            paths.append(wav)
        return paths

    def say(self, text):
        """synth() + play each chunk on this machine's speakers."""
        from .audio import play
        for wav in self.synth(text):
            play(wav, self.hooks)
