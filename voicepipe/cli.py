"""The command-line surface shared by every entrypoint.

chat_loop.py and bridge_server.py select the same pipeline (STT -> LLM ->
TTS) and differ only in where the audio comes from, so the flags that choose
and configure that pipeline are built once, here. Each backend contributes
its own options through the registry, which is what keeps backend names out
of the entrypoints entirely.
"""
import argparse
import os
import sys

from . import backends  # noqa: F401 - registers every backend as a side effect
from .personas import DEFAULT_PERSONA, PERSONAS, PROFILE_PATH, compose, load_profile, resolve
from .registry import LLM, STT, SV, TTS
from .speaker import (
    DEFAULT_THRESHOLD, SHORT_ASK, SHORT_POLICIES, SpeakerGate, Voiceprint,
)


def build_parser(description):
    """A parser carrying the shared pipeline flags plus every backend's own."""
    ap = argparse.ArgumentParser(description=description,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)

    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                    help="Ollama base URL (env OLLAMA_HOST), e.g. http://media:11434")
    ap.add_argument("--model", default="rina", help="Ollama model name (default: rina)")
    ap.add_argument("--persona", default=DEFAULT_PERSONA, choices=sorted(PERSONAS),
                    help="built-in system prompt: 'partner' (romantic companion) or "
                         f"'assistant' (neutral helper) (default: {DEFAULT_PERSONA})")
    ap.add_argument("--system", default=None,
                    help="override --persona with a custom system prompt; pass --system '' to send "
                         "none at all, e.g. when the Ollama model already carries its own persona")
    ap.add_argument("--think", action="store_true",
                    help="let reasoning models (qwen3, r1, ...) do their <think> pass — "
                         "more coherent, much slower")
    ap.add_argument("--profile", default=PROFILE_PATH, metavar="ABOUT_ME.MD",
                    help=f"markdown file of facts about you, added to the system prompt so "
                         f"she doesn't have to be told them again (default: {PROFILE_PATH})")
    ap.add_argument("--no-profile", action="store_true",
                    help="ignore the profile file even if it exists")

    ap.add_argument("--llm-backend", default="ollama", choices=LLM.names(),
                    help="LLM backend (default: ollama)")
    ap.add_argument("--stt-backend", default="faster-whisper", choices=STT.names(),
                    help="STT backend (default: faster-whisper)")
    ap.add_argument("--tts-backend", default="chatterbox", choices=TTS.names(),
                    help="TTS backend (default: chatterbox)")
    ap.add_argument("--stt-lang", default="en",
                    help="STT language: en, ja, ... or 'auto' to detect per utterance (default: en)")
    ap.add_argument("--no-voice", action="store_true", help="text only, skip TTS entirely")

    ap.add_argument("--speaker-backend", default="wespeaker", choices=SV.names(),
                    help="speaker-verification backend (default: wespeaker)")
    ap.add_argument("--speaker-threshold", type=float, default=None,
                    help="cosine score an utterance must beat to be accepted; defaults to "
                         "whatever the chosen backend measured for itself. Every utterance "
                         "logs its score, so tune this from what you actually see")
    ap.add_argument("--speaker-min-seconds", type=float, default=None,
                    help="below this many seconds an utterance isn't judged at all and the "
                         "speaker is asked to repeat; defaults to the backend's own value")
    ap.add_argument("--short-utterances", default=SHORT_ASK, choices=SHORT_POLICIES,
                    help="what to do with a clip too brief to verify: 'ask' for a longer "
                         "one (default, safe), or 'allow' to let it through unjudged — "
                         "convenient for short commands, but anyone can then get past the "
                         "gate by keeping it brief")
    ap.add_argument("--no-speaker-check", action="store_true",
                    help="answer anyone, even with a voiceprint enrolled")

    for registry in (LLM, STT, TTS, SV):
        registry.add_arguments(ap)
    return ap


def parse_args(parser):
    """Parse, then fold --persona/--system/--profile into one args.system."""
    args = parser.parse_args()
    system = resolve(args.persona, args.system)
    profile = None if args.no_profile else load_profile(args.profile)
    if profile:
        print(f"  profile: {args.profile} ({len(profile)} chars)")
    args.system = compose(system, profile)
    return args


def build_llm(args):
    """The chosen LLM backend, connectivity-checked when it supports it."""
    llm = LLM.build(args.llm_backend, args)
    check = getattr(llm, "check", None)
    if check is not None:
        try:
            warning = check()
        except RuntimeError as e:  # OllamaUnavailable and friends
            sys.exit(f"  ! {e}")
        if warning:
            print(warning)
    return llm


def build_stt(args):
    """The chosen STT backend, plus the language to transcribe in."""
    stt = STT.build(args.stt_backend, args)
    return stt, (None if args.stt_lang == "auto" else args.stt_lang)


def build_speaker_gate(args):
    """The speaker gate, off unless a voiceprint has been enrolled.

    The model is only loaded when there is a voiceprint to compare against, so
    an un-enrolled setup pays nothing — no download, no onnxruntime import.
    """
    short_policy = getattr(args, "short_utterances", SHORT_ASK)
    voiceprint = Voiceprint.load()
    if args.no_speaker_check or not voiceprint:
        if voiceprint and args.no_speaker_check:
            print("  speaker check disabled (--no-speaker-check)")
        return SpeakerGate(None, voiceprint, args.speaker_threshold, args.speaker_min_seconds,
                           short_policy)

    gate = SpeakerGate(SV.build(args.speaker_backend, args), voiceprint,
                       args.speaker_threshold, args.speaker_min_seconds, short_policy)
    if gate.mismatch:
        # Loud, because the alternative is a gate that looks on but is off.
        print(f"  ! speaker gate OFF: {gate.mismatch}")
    else:
        print(f"  speaker gate on — {len(voiceprint)} enrolled samples, "
              f"backend {args.speaker_backend}, threshold {gate.threshold}, "
              f"min {gate.min_verify_seconds}s")
        if gate.short_policy != SHORT_ASK:
            # Loud, because it is the one configuration in which the gate can
            # be walked past without scoring anything at all.
            print(f"  ! anything under {gate.min_verify_seconds}s is allowed through "
                  f"UNVERIFIED (--short-utterances allow)")
    return gate


def build_tts(args):
    """The chosen TTS backend, already started, or None with --no-voice.

    Starting here rather than on the first reply means the model-load cost is
    paid at startup, where it's visible, instead of as a delay mid-conversation.
    """
    if args.no_voice:
        return None
    voice = TTS.build(args.tts_backend, args)
    start = getattr(voice, "start", None)
    if start is not None:
        start()
    return voice
