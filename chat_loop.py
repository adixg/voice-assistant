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
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UMA_PY = os.path.expanduser("~/anaconda3/envs/uma-tts/bin/python")
TTS_CLI = os.path.join(HERE, "tts_cli.py")
TMP = tempfile.gettempdir()


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


# ---------------------------------------------------------------- audio capture
def record(path):
    """Record from the default pulse source until the user presses Enter."""
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "pulse", "-i", "default", "-ac", "1", "-ar", "16000", path],
        stdin=subprocess.DEVNULL,
    )
    try:
        input("  ● recording — Enter to stop ")
    finally:
        proc.send_signal(signal.SIGINT)  # lets ffmpeg flush the wav trailer
        proc.wait()
    return path


def play(path):
    subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path])


# ---------------------------------------------------------------- STT
def _silent_wav():
    import struct
    import wave
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


def ask(client, model, messages):
    resp = client.chat(model=model, messages=messages)
    return strip_think(resp["message"]["content"])


# ---------------------------------------------------------------- TTS
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


def speak(text, args):
    for i, chunk in enumerate(chunks(text)):
        wav = os.path.join(TMP, f"rina_reply_{i}.wav")
        cmd = [UMA_PY, TTS_CLI, chunk, "-m", args.tts_model,
               "-s", str(args.speaker), "-o", wav, "--device", args.tts_device]
        if args.tts_model == "trilingual":
            cmd += ["-l", args.tts_lang]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("  (tts failed)\n" + (r.stderr or r.stdout)[-800:])
            return
        play(wav)


# ---------------------------------------------------------------- main loop
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                    help="Ollama base URL (env OLLAMA_HOST), e.g. http://media:11434")
    ap.add_argument("--model", default="rina", help="Ollama model name (default: rina)")
    ap.add_argument("--system", default=None, help="optional system prompt override")
    ap.add_argument("--whisper-model", default="small", help="faster-whisper size (tiny/base/small/medium/large-v3)")
    ap.add_argument("--whisper-device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--stt-lang", default=None, help="force STT language (e.g. en, ja); default: autodetect")
    ap.add_argument("--tts-model", default="trilingual", choices=["trilingual", "japanese"])
    ap.add_argument("--tts-lang", default="en", choices=["ja", "zh", "en", "mix", "none"],
                    help="language token for the trilingual VITS model")
    ap.add_argument("--tts-device", default="cuda", help="torch device for VITS (cuda/cpu)")
    ap.add_argument("--speaker", type=int, default=10, help="VITS speaker id")
    ap.add_argument("--no-voice", action="store_true", help="text only, skip VITS")
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

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

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
            speak(cmd[5:], args)
            continue
        if cmd.startswith("/text "):
            user_text = cmd[6:].strip()
        elif cmd == "":
            wav = record(os.path.join(TMP, "chat_in.wav"))
            t0 = time.time()
            user_text = transcribe(stt, wav, args.stt_lang)
            print(f"  you said ({time.time()-t0:.1f}s): {user_text or '(nothing heard)'}")
            if not user_text:
                continue
        else:
            print("  unknown command; press Enter to talk or use /text <msg>")
            continue

        messages.append({"role": "user", "content": user_text})
        try:
            reply = ask(client, args.model, messages)
        except Exception as e:  # noqa: BLE001
            print(f"  ! ollama error: {e}")
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": reply})
        print(f"\n{args.model}> {reply}\n")
        if not args.no_voice:
            speak(reply, args)


if __name__ == "__main__":
    main()
