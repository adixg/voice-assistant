#!/usr/bin/env python3
"""Voice chat with a remote Ollama model, replies in text + VITS speech.

Pipeline:  mic --ffmpeg--> faster-whisper (STT) --> Ollama (multi-turn) --> print + VITS --> ffplay
The STT/LLM/TTS pieces live in voicepipe/ and are importable/testable on their
own; this file is just the local-mic terminal+orb entrypoint around them.

Run in the `chat` conda env (has faster-whisper + ollama).
Needs on PATH: ffmpeg, ffplay.  Needs the `uma-tts` env for tts_cli.py (shelled out).

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
import argparse
import os
import queue
import signal
import sys
import tempfile
import threading
import time

from voicepipe.audio import Hooks, record
from voicepipe.llm import DEFAULT_PERSONA, PERSONAS  # also registers the "ollama" LLM backend
from voicepipe.registry import LLM, STT, TTS
from voicepipe.stt import ensure_cuda_libs  # also registers the "faster-whisper" STT backend
import voicepipe.tts  # noqa: F401 - also registers the "vits" TTS backend

TMP = tempfile.gettempdir()

ensure_cuda_libs()


# ---------------------------------------------------------------- orb glue
class Gate:
    """One queue fed by both the terminal (a stdin reader thread) and the orb
    window (its Enter key). converse() and record() block on .get() for the next
    line; the orb pushes "" to mean 'toggle talking'."""

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
                voice.say(cmd[5:])
                hooks.state("idle")
            else:
                print(cmd[5:])
            continue
        if cmd.startswith("/text "):
            user_text = cmd[6:].strip()
        elif cmd == "":
            hooks.state("listening")
            print("  ● recording — Enter to stop")
            wav = record(os.path.join(TMP, "chat_in.wav"), hooks, gate)
            hooks.state("thinking")
            t0 = time.time()
            user_text = stt.transcribe(wav, stt_lang)
            print(f"  you said ({time.time()-t0:.1f}s): {user_text or '(nothing heard)'}")
            if not user_text:
                hooks.state("idle")
                continue
        else:
            print("  unknown command; press Enter to talk or use /text <msg>")
            continue

        hooks.state("thinking")
        messages.append({"role": "user", "content": user_text})
        try:
            reply = llm.ask(messages, None if args.think else False)
        except Exception as e:  # noqa: BLE001
            print(f"  ! llm error: {e}")
            messages.pop()
            hooks.state("idle")
            continue
        messages.append({"role": "assistant", "content": reply})
        print(f"\nAI GF> {reply}\n")
        if voice:
            hooks.state("speaking")
            voice.say(reply)
        hooks.state("idle")

    if voice:
        voice.close()


def run_with_orb(llm, stt, stt_lang, voice, args):
    """Show the speech orb on the main thread; run `converse` on a worker."""
    from PySide6.QtCore import QObject, QThread, QTimer, Signal
    from PySide6.QtWidgets import QApplication
    from speech_orb import SpeechOrb

    app = QApplication.instance() or QApplication([])

    class QtHooks(QObject):
        _s = Signal(str)
        _l = Signal(float)

        def state(self, name):
            self._s.emit(name)

        def level(self, x):
            self._l.emit(float(x))

    try:
        from speech_orb import BASE_BG
    except ImportError:
        BASE_BG = "#24273a"

    gate = Gate()

    orb = SpeechOrb()
    orb.setWindowTitle(args.model)
    orb.setStyleSheet(f"background:{BASE_BG};")
    orb.resize(360, 360)
    orb.on_enter = gate.push            # Enter/Space in the orb window == Enter in the terminal
    orb.show()
    orb.raise_()
    orb.activateWindow()
    orb.setFocus()

    hooks = QtHooks()
    hooks._s.connect(orb.set_state)      # cross-thread -> queued to the GUI thread
    hooks._l.connect(orb.push_level)
    if voice:
        voice.hooks = hooks

    class Worker(QThread):
        def run(self):
            converse(llm, stt, stt_lang, voice, args, hooks, gate)

    def shutdown():
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                    help="Ollama base URL (env OLLAMA_HOST), e.g. http://media:11434")
    ap.add_argument("--model", default="rina", help="Ollama model name (default: rina)")
    ap.add_argument("--persona", default=DEFAULT_PERSONA, choices=sorted(PERSONAS),
                    help=f"built-in system prompt: 'partner' (romantic companion) or "
                         f"'assistant' (neutral helper) (default: {DEFAULT_PERSONA})")
    ap.add_argument("--system", default=None,
                    help="override --persona with a custom system prompt; pass --system '' to send "
                         "none at all, e.g. when the Ollama model already has its own persona")
    ap.add_argument("--think", action="store_true",
                    help="let reasoning models (qwen3, r1...) do their <think> pass — more coherent, much slower")
    ap.add_argument("--llm-backend", default="ollama", choices=LLM.names(),
                    help="LLM backend (default: ollama)")
    ap.add_argument("--stt-backend", default="faster-whisper", choices=STT.names(),
                    help="STT backend (default: faster-whisper)")
    ap.add_argument("--tts-backend", default="vits", choices=TTS.names(),
                    help="TTS backend (default: vits)")
    ap.add_argument("--whisper-model", default="small", help="faster-whisper size (tiny/base/small/medium/large-v3)")
    ap.add_argument("--whisper-device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--stt-lang", default="en",
                    help="STT language: en, ja, ... or 'auto' to detect per utterance (default: en)")
    ap.add_argument("--tts-model", default="trilingual", choices=["trilingual", "japanese"])
    ap.add_argument("--tts-lang", default="en", choices=["ja", "zh", "en", "mix", "none"],
                    help="language token for the trilingual VITS model")
    ap.add_argument("--tts-device", default="cpu",
                    help="torch device for VITS (default cpu: ~0.8s and leaves the 4GB GPU to whisper; "
                         "use cuda if VRAM is free)")
    ap.add_argument("--speaker", type=int, default=10, help="VITS speaker id")
    ap.add_argument("--no-voice", action="store_true", help="text only, skip VITS")
    ap.add_argument("--no-orb", action="store_true",
                    help="don't show the animated speech orb (also skipped when no display / PySide6)")
    args = ap.parse_args()
    if args.system is None:
        args.system = PERSONAS[args.persona]

    llm = LLM.create(args.llm_backend, host=args.host, model=args.model)
    if hasattr(llm, "check"):
        llm.check()

    stt = STT.create(args.stt_backend, model=args.whisper_model, device=args.whisper_device)
    stt_lang = None if args.stt_lang == "auto" else args.stt_lang

    voice = None
    if not args.no_voice:
        voice = TTS.create(args.tts_backend, args)
        if hasattr(voice, "start"):
            voice.start()  # pay the model-load cost now, not on the first reply

    want_orb = not args.no_orb and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if want_orb:
        try:
            run_with_orb(llm, stt, stt_lang, voice, args)
            return
        except ImportError as e:
            print(f"  (orb off: {e}); running in the terminal only")

    converse(llm, stt, stt_lang, voice, args, Hooks())


if __name__ == "__main__":
    main()
