#!/usr/bin/env python3
"""Voice chat with an Ollama model in the terminal, replying in text and speech.

Pipeline:  mic --ffmpeg--> STT --> LLM (multi-turn) --> print + TTS --> ffplay
The STT/LLM/TTS pieces live in voicepipe/ and are importable and testable on
their own; this file is just the local-mic entrypoint around them. See
bridge_server.py for the M5StickS3 version.

Run in the `chat` conda env. Needs ffmpeg and ffplay on PATH, plus whichever
conda env the chosen --tts-backend shells out to. Run with --help to see
every backend's own options.

    conda activate chat
    python chat_loop.py --host http://media:11434 --model rina

Per turn: press Enter to start talking, Enter again to stop.
In-loop commands:  /text <msg>   type instead of speak
                   /say <text>   just test the voice
                   /reset        clear conversation history
                   /quit

An animated "speech orb" window (speech_orb.py) shows idle/listening/thinking/
speaking and pulses with the live mic and TTS levels. It appears automatically
when a display and PySide6 are available; --no-orb keeps everything in the
terminal. The terminal prompts still work with the orb up.
"""
import os
import queue
import signal
import sys
import tempfile
import threading
import time

from voicepipe import cli
from voicepipe.audio import Hooks, record
from voicepipe.cuda import ensure_cuda_libs
from voicepipe.registry import DELTA, FINAL, STATUS, stream_reply

ensure_cuda_libs()
sys.stdout.reconfigure(line_buffering=True)  # see bridge_server.py

TMP = tempfile.gettempdir()


# ---------------------------------------------------------------- orb glue
class Gate:
    """One queue fed by both the terminal (a stdin reader thread) and the orb
    window (its Enter key). converse() and record() block on .get() for the
    next line; the orb pushes "" to mean 'toggle talking'."""

    def __init__(self):
        self._q = queue.Queue()

    def push(self, item=""):
        self._q.put(item)

    def get(self):
        return self._q.get()


def _stdin_pump(gate):
    try:
        for line in sys.stdin:
            gate.push(line.rstrip("\n"))
    except Exception:  # noqa: BLE001
        pass
    gate.push(None)  # EOF / Ctrl-D sentinel


# ---------------------------------------------------------------- conversation
def ask_with_progress(llm, messages, args, hooks):
    """The model's reply, showing progress while it is produced.

    With a plain chat model this is one FINAL event and behaves exactly like
    `llm.ask()`. With an agent it also surfaces each tool it runs, in the
    terminal and in the orb's caption, so a 25-second turn doesn't look like a
    hang. DELTA chunks are printed as they arrive for a live-typing effect.
    """
    reply = ""
    streamed = False
    for kind, payload in stream_reply(llm, messages, None if args.think else False):
        if kind == STATUS:
            print(f"\n  ... {payload}", flush=True)
            hooks.caption(payload)
        elif kind == DELTA:
            if not streamed:
                sys.stdout.write("\nAI GF> ")
                streamed = True
            sys.stdout.write(payload)
            sys.stdout.flush()
        elif kind == FINAL:
            reply = payload
    # This function owns printing the reply either way, so a streamed answer
    # isn't printed twice — once live and once whole.
    print("\n" if streamed else f"\nAI GF> {reply}\n")
    return reply


def converse(llm, stt, stt_lang, voice, args, hooks, gate=None):
    """The blocking talk/listen/think/speak loop. Runs on the main thread when
    headless, or on a worker thread when the orb window is up. Input lines come
    through `gate` — fed by a stdin reader and, in orb mode, the orb's Enter key."""
    if gate is None:
        gate = Gate()
    threading.Thread(target=_stdin_pump, args=(gate,), daemon=True).start()

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    hooks.state("idle")
    print("\nready. Enter (here or in the orb) = talk, /text <msg>, /say <text>, /reset, /quit\n")
    while True:
        sys.stdout.write("you> ")
        sys.stdout.flush()
        try:
            cmd = gate.get()
        except KeyboardInterrupt:
            print()
            break
        if cmd is None:            # Ctrl-D / stdin closed
            print()
            break
        cmd = cmd.strip()

        if cmd in ("/quit", "/exit", "/q"):
            break
        if cmd == "/reset":
            messages = [m for m in messages if m["role"] == "system"]
            print("  (history cleared)")
            continue
        if cmd.startswith("/say "):
            if voice:
                hooks.state("speaking")
                hooks.caption(cmd[5:])
                voice.say(cmd[5:])
                hooks.state("idle")
                hooks.caption("")
            else:
                print(cmd[5:])
            continue
        if cmd.startswith("/text "):
            user_text = cmd[6:].strip()
        elif cmd == "":
            hooks.state("listening")
            hooks.caption("")
            print("  ● recording — Enter to stop")
            wav = record(os.path.join(TMP, "chat_in.wav"), hooks, gate)
            hooks.state("thinking")
            t0 = time.time()
            user_text = stt.transcribe(wav, stt_lang)
            print(f"  you said ({time.time()-t0:.1f}s): {user_text or '(nothing heard)'}")
            if not user_text:
                hooks.state("idle")
                continue
            hooks.caption(user_text)  # the transcript stays up while she thinks
        else:
            print("  unknown command; press Enter to talk or use /text <msg>")
            continue

        hooks.state("thinking")
        messages.append({"role": "user", "content": user_text})
        try:
            reply = ask_with_progress(llm, messages, args, hooks)
        except Exception as e:  # noqa: BLE001
            print(f"  ! llm error: {e}")
            messages.pop()
            hooks.state("idle")
            continue
        messages.append({"role": "assistant", "content": reply})
        if voice:
            hooks.state("speaking")
            hooks.caption(reply)
            voice.say(reply)
        hooks.state("idle")
        hooks.caption("")


def run_with_orb(llm, stt, stt_lang, voice, args):
    """Show the speech orb on the main thread; run `converse` on a worker."""
    from PySide6.QtCore import QObject, QThread, QTimer, Signal
    from PySide6.QtWidgets import QApplication
    from speech_orb import SpeechOrb

    app = QApplication.instance() or QApplication([])

    class QtHooks(QObject):
        _s = Signal(str)
        _l = Signal(float)
        _c = Signal(str)

        def state(self, name):
            self._s.emit(name)

        def level(self, x):
            self._l.emit(float(x))

        def caption(self, text):
            self._c.emit(text)

    try:
        from speech_orb import BASE_BG
    except ImportError:
        BASE_BG = "#24273a"

    gate = Gate()

    orb = SpeechOrb()
    orb.setWindowTitle(args.model)
    orb.setStyleSheet(f"background:{BASE_BG};")
    orb.resize(360, 360)
    orb.on_enter = gate.push            # Enter/Space in the orb == Enter in the terminal
    orb.show()
    orb.raise_()
    orb.activateWindow()
    orb.setFocus()

    hooks = QtHooks()
    hooks._s.connect(orb.set_state)      # cross-thread -> queued to the GUI thread
    hooks._l.connect(orb.push_level)
    hooks._c.connect(orb.set_caption)
    if voice:
        voice.hooks = hooks

    class Worker(QThread):
        def run(self):
            converse(llm, stt, stt_lang, voice, args, hooks, gate)

    def shutdown():
        # os._exit skips finally blocks (it's here because Qt + the worker
        # thread otherwise hang on exit), so the worker has to be stopped
        # explicitly before it. close() is idempotent, so main()'s finally
        # closing it again is harmless.
        try:
            if voice:
                voice.close()
        finally:
            os._exit(0)

    app.aboutToQuit.connect(shutdown)
    signal.signal(signal.SIGINT, signal.SIG_DFL)   # let Ctrl-C kill it
    keepalive = QTimer()
    keepalive.start(200)
    keepalive.timeout.connect(lambda: None)         # keep the interpreter ticking

    worker = Worker()
    worker.finished.connect(app.quit)
    worker.start()
    app.exec()


# ---------------------------------------------------------------- main
def main():
    ap = cli.build_parser(__doc__)
    ap.add_argument("--no-orb", action="store_true",
                    help="don't show the animated speech orb (also skipped when no display / PySide6)")
    args = cli.parse_args(ap)

    llm = cli.build_llm(args)
    stt, stt_lang = cli.build_stt(args)
    voice = cli.build_tts(args)

    try:
        want_orb = not args.no_orb and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if want_orb:
            try:
                run_with_orb(llm, stt, stt_lang, voice, args)
                return
            except ImportError as e:
                print(f"  (orb off: {e}); running in the terminal only")
        converse(llm, stt, stt_lang, voice, args, Hooks())
    finally:
        # One place that shuts the TTS worker down, whichever way we leave —
        # orb or terminal, clean exit or exception.
        if voice:
            voice.close()


if __name__ == "__main__":
    main()
