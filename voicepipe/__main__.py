"""Run one stage of the pipeline on its own — the quickest way to find out
which of STT, LLM or TTS is misbehaving, without a mic, a Stick, or the
other two stages.

    python -m voicepipe speak "hello there"
    python -m voicepipe speak "hello there" --tts-backend vits --vits-speaker 10
    python -m voicepipe transcribe /tmp/hi.wav
    python -m voicepipe ask "hi there" --model rina

Every backend's own flags are accepted here too (see --help), and any backend
added to voicepipe/backends/ appears in these commands automatically.
"""
import argparse
import sys

from . import backends  # noqa: F401 - registers every backend
from .personas import DEFAULT_PERSONA, PERSONAS, resolve
from .registry import LLM, STT, TTS


def cmd_speak(args):
    """Synthesize text and print the wav paths (doesn't play them)."""
    voice = TTS.build(args.tts_backend, args)
    try:
        for path in voice.synth(args.text):
            print(path)
    finally:
        voice.close()


def cmd_transcribe(args):
    """Transcribe a wav file."""
    stt = STT.build(args.stt_backend, args)
    print(stt.transcribe(args.wav, None if args.lang == "auto" else args.lang))


def cmd_ask(args):
    """One LLM turn, with the chosen persona."""
    llm = LLM.build(args.llm_backend, args)
    system = resolve(args.persona, args.system)
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": args.text})
    print(llm.ask(messages, None if args.think else False))


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m voicepipe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    speak = sub.add_parser("speak", help="text -> wav via a TTS backend")
    speak.add_argument("text")
    speak.add_argument("--tts-backend", default="vits", choices=TTS.names())
    TTS.add_arguments(speak)
    speak.set_defaults(func=cmd_speak)

    transcribe = sub.add_parser("transcribe", help="wav -> text via an STT backend")
    transcribe.add_argument("wav")
    transcribe.add_argument("--lang", default="en", help="language code, or 'auto' to detect")
    transcribe.add_argument("--stt-backend", default="faster-whisper", choices=STT.names())
    STT.add_arguments(transcribe)
    transcribe.set_defaults(func=cmd_transcribe)

    ask = sub.add_parser("ask", help="one LLM turn")
    ask.add_argument("text")
    ask.add_argument("--host", default="http://localhost:11434")
    ask.add_argument("--model", default="rina")
    ask.add_argument("--persona", default=DEFAULT_PERSONA, choices=sorted(PERSONAS))
    ask.add_argument("--system", default=None, help="override --persona; pass '' to send none")
    ask.add_argument("--think", action="store_true")
    ask.add_argument("--llm-backend", default="ollama", choices=LLM.names())
    LLM.add_arguments(ask)
    ask.set_defaults(func=cmd_ask)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
