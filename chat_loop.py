#!/usr/bin/env python3
"""Voice chat with a remote Ollama model, replies in text + VITS speech.

Pipeline:  mic --ffmpeg--> faster-whisper (STT) --> Ollama (multi-turn) --> print + VITS --> ffplay

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
import array
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
UMA_PY = os.path.expanduser("~/anaconda3/envs/uma-tts/bin/python")
TTS_CLI = os.path.join(HERE, "tts_cli.py")
TMP = tempfile.gettempdir()

DEFAULT_SYSTEM = (
    "You are Rina, the user's warm, playful, affectionate companion. "
    "Talk in casual, everyday language and keep replies to one or two sentences. "
    "Never narrate your own thoughts, never use stage directions, never use emojis. "
    "Stay in character and don't mention being an AI."
)


def _ensure_cuda_libs():
    """ctranslate2 dlopen's libcublas/libcudnn from the nvidia-*-cu12 wheels, but
    only if they're on LD_LIBRARY_PATH at process start. Add them and re-exec once."""
    if os.environ.get("_CHAT_LOOP_REEXEC") or not os.path.isfile(sys.argv[0]):
        return
    import importlib.util
    import pathlib
    dirs = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            d = pathlib.Path(spec.submodule_search_locations[0]) / "lib"
            if d.is_dir():
                dirs.append(str(d))
    if not dirs:
        return
    cur = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + ([cur] if cur else []))
    os.environ["_CHAT_LOOP_REEXEC"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])


_ensure_cuda_libs()


# ---------------------------------------------------------------- orb hooks
class Hooks:
    """No-op sink for orb state/level updates; the headless run uses this as-is."""

    def state(self, name):
        pass

    def level(self, x):
        pass


def _rms16(raw, gain=8.0):
    """Normalised 0..1 loudness of a little-endian s16 mono buffer."""
    if len(raw) < 2:
        return 0.0
    a = array.array("h")
    a.frombytes(raw[: len(raw) & ~1])
    if not a:
        return 0.0
    mean_sq = sum(v * v for v in a) / len(a)
    return min(1.0, (math.sqrt(mean_sq) / 32768.0) * gain)


# ---------------------------------------------------------------- audio capture
def record(path, hooks=Hooks()):
    """Record the default pulse source until the user presses Enter, streaming the
    live level to `hooks` and writing a 16 kHz mono wav at `path`."""
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "pulse", "-i", "default", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    buf = bytearray()

    def pump():
        while True:
            chunk = proc.stdout.read(3200)  # ~0.1 s
            if not chunk:
                break
            buf.extend(chunk)
            hooks.level(_rms16(chunk))

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        input("  ● recording — Enter to stop ")
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait()
        t.join(timeout=1.0)
        hooks.level(0.0)

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(buf))
    return path


def play(path, hooks=Hooks()):
    """Play `path` with ffplay, pushing per-frame level to `hooks` in real time."""
    try:
        with wave.open(path, "rb") as w:
            sr, sw = w.getframerate(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001
        raw, sr, sw = b"", 22050, 2

    proc = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path],
        stdin=subprocess.DEVNULL,
    )
    if raw and sw == 2:
        step = max(1, int(sr * 0.03)) * 2  # 30 ms of s16
        t0 = time.time()
        for off in range(0, len(raw), step):
            hooks.level(_rms16(raw[off:off + step]))
            slp = t0 + (off / 2) / sr - time.time()
            if slp > 0:
                time.sleep(slp)
            if proc.poll() is not None:
                break
    proc.wait()
    hooks.level(0.0)


# ---------------------------------------------------------------- STT
def _silent_wav():
    import struct
    path = os.path.join(TMP, "stt_probe.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<1600h", *([0] * 1600)))  # 0.1s silence
    return path


def load_stt(model_name, want_device):
    from faster_whisper import WhisperModel
    tries = []
    if want_device in ("auto", "cuda"):
        tries.append(("cuda", "float16"))
    if want_device in ("auto", "cpu"):
        tries.append(("cpu", "int8"))
    probe = _silent_wav()
    last = None
    for dev, ct in tries:
        try:
            m = WhisperModel(model_name, device=dev, compute_type=ct)
            # ctranslate2 loads its CUDA libs lazily, so force a real inference now
            list(m.transcribe(probe, without_timestamps=True)[0])
            print(f"  faster-whisper '{model_name}' on {dev} ({ct})")
            return m
        except Exception as e:  # noqa: BLE001 - cuda libs missing, etc.
            last = e
            if want_device == "auto" and dev == "cuda":
                print(f"  (whisper cuda unavailable: {str(e).splitlines()[0]}; falling back to cpu)")
    raise last


def transcribe(model, path, lang):
    segs, _ = model.transcribe(path, language=lang or None, vad_filter=True)
    return " ".join(s.text.strip() for s in segs).strip()


# ---------------------------------------------------------------- LLM
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


# ---------------------------------------------------------------- TTS
class Voice:
    """Long-lived `tts_cli.py --serve` subprocess so the VITS model loads once."""

    def __init__(self, args, hooks=None):
        self.args = args
        self.hooks = hooks or Hooks()
        self.p = None
        self.logpath = os.path.join(TMP, "tts_worker.log")

    def start(self):
        a = self.args
        cmd = [UMA_PY, TTS_CLI, "--serve", "-m", a.tts_model,
               "-s", str(a.speaker), "--device", a.tts_device]
        if a.tts_model == "trilingual":
            cmd += ["-l", a.tts_lang]
        print(f"  starting VITS worker on {a.tts_device} ...", flush=True)
        self.p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(self.logpath, "w"), text=True, bufsize=1,
        )
        line = self.p.stdout.readline().strip()
        if line != "ready":
            tail = ""
            try:
                tail = open(self.logpath).read()[-800:]
            except OSError:
                pass
            raise RuntimeError(f"VITS worker failed to start ({line!r})\n{tail}")

    def say(self, text):
        if self.p is None or self.p.poll() is not None:
            self.start()
        for i, chunk in enumerate(chunks(text)):
            wav = os.path.join(TMP, f"rina_reply_{i}.wav")
            self.p.stdin.write(f"{wav}\t{chunk.replace(chr(10), ' ')}\n")
            self.p.stdin.flush()
            resp = self.p.stdout.readline().strip()
            if resp != wav:
                print(f"  (tts: {resp or 'worker died'})")
                return
            play(wav, self.hooks)

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.close()
            except OSError:
                pass
            self.p.terminate()


def chunks(text, limit=200):
    # sentence split, then hard-wrap any sentence that is still too long
    pieces = []
    for sent in re.split(r"(?<=[.!?。．！？])\s+", text.strip()):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > limit:
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if sent:
            pieces.append(sent)

    parts, buf = [], ""
    for piece in pieces:
        if len(buf) + len(piece) + 1 > limit and buf:
            parts.append(buf)
            buf = piece
        else:
            buf = f"{buf} {piece}".strip()
    if buf:
        parts.append(buf)
    return parts or [text.strip()]


# ---------------------------------------------------------------- conversation
def converse(client, stt, stt_lang, voice, args, hooks):
    """The blocking talk/listen/think/speak loop. Runs on the main thread when
    headless, or on a worker thread when the orb window is up."""
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    hooks.state("idle")
    print("\nready. Enter = talk, /text <msg>, /say <text>, /reset, /quit\n")
    while True:
        try:
            cmd = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

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
            wav = record(os.path.join(TMP, "chat_in.wav"), hooks)
            hooks.state("thinking")
            t0 = time.time()
            user_text = transcribe(stt, wav, stt_lang)
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
            reply = ask(client, args.model, messages, None if args.think else False)
        except Exception as e:  # noqa: BLE001
            print(f"  ! ollama error: {e}")
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


def run_with_orb(client, stt, stt_lang, voice, args):
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

    orb = SpeechOrb()
    orb.setWindowTitle(args.model)
    orb.setStyleSheet("background:#0e1116;")
    orb.resize(360, 360)
    orb.show()

    hooks = QtHooks()
    hooks._s.connect(orb.set_state)      # cross-thread -> queued to the GUI thread
    hooks._l.connect(orb.push_level)
    if voice:
        voice.hooks = hooks

    class Worker(QThread):
        def run(self):
            converse(client, stt, stt_lang, voice, args, hooks)

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
    ap.add_argument("--system", default=DEFAULT_SYSTEM,
                    help="system prompt (defaults to the Rina persona); pass --system '' to send none, "
                         "e.g. when the Ollama model already has its own persona")
    ap.add_argument("--think", action="store_true",
                    help="let reasoning models (qwen3, r1...) do their <think> pass — more coherent, much slower")
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

    import socket
    from urllib.parse import urlparse
    import ollama

    u = urlparse(args.host)
    try:
        with socket.create_connection((u.hostname, u.port or 11434), timeout=4):
            pass
    except OSError as e:
        print(f"  ! can't reach {args.host}: {e}")
        print("    check `tailscale status` (peer online?) and that `ollama serve` is up there,")
        print("    and that OLLAMA_HOST=0.0.0.0 on that box so it listens on the tailnet.")
        sys.exit(1)

    client = ollama.Client(host=args.host, timeout=120)
    try:
        names = [m.model for m in client.list().models]
        print(f"  ollama @ {args.host} — {len(names)} models")
        if args.model not in names and f"{args.model}:latest" not in names:
            print(f"  ! '{args.model}' not found. available: {', '.join(names) or '(none)'}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! ollama on {args.host} answered but /api/tags failed: {e}")
        sys.exit(1)

    stt = load_stt(args.whisper_model, args.whisper_device)
    stt_lang = None if args.stt_lang == "auto" else args.stt_lang

    voice = None
    if not args.no_voice:
        voice = Voice(args)
        voice.start()  # pay the model-load cost now, not on the first reply

    want_orb = not args.no_orb and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if want_orb:
        try:
            run_with_orb(client, stt, stt_lang, voice, args)
            return
        except ImportError as e:
            print(f"  (orb off: {e}); running in the terminal only")

    converse(client, stt, stt_lang, voice, args, Hooks())


if __name__ == "__main__":
    main()
