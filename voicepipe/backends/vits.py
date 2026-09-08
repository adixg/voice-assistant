"""VITS-Umamusume — the original voice, kept as the CPU-friendly alternative.

Runs in the `uma-tts` conda env (requirements-uma-tts.txt) via tts_cli.py,
and needs the cloned Space at VITS-Umamusume-voice-synthesizer/. Fast enough
on CPU to leave the GPU entirely to whisper, which is why it stays useful
even though Chatterbox is the default.

Standalone debug use (writes wavs, doesn't play them):
    python -m voicepipe speak "hello there" --tts-backend vits --vits-speaker 10
"""
import os

from ..registry import TTS
from ..subproc import WorkerVoice

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.expanduser("~/anaconda3/envs/uma-tts/bin/python")
CLI = os.path.join(REPO, "tts_cli.py")

MODELS = ["trilingual", "japanese"]
LANGS = ["ja", "zh", "en", "mix", "none"]


@TTS.register("vits")
class VitsVoice(WorkerVoice):
    name = "vits"

    def __init__(self, model="trilingual", lang="en", speaker=10, device="cpu", hooks=None):
        super().__init__(hooks=hooks)
        self.model = model
        self.lang = lang
        self.speaker = speaker
        self.device = device

    @staticmethod
    def add_arguments(group):
        # The historical flag names are kept as aliases: they predate the
        # per-backend naming and are what the README and muscle memory use.
        group.add_argument("--vits-model", "--tts-model", dest="vits_model",
                           default="trilingual", choices=MODELS,
                           help="'trilingual' (JP/ZH/EN) or 'japanese' (JP only)")
        group.add_argument("--vits-lang", "--tts-lang", dest="vits_lang",
                           default="en", choices=LANGS,
                           help="language token for the trilingual VITS model")
        group.add_argument("--vits-speaker", "--speaker", dest="vits_speaker",
                           type=int, default=10, help="VITS speaker id")
        group.add_argument("--vits-device", "--tts-device", dest="vits_device",
                           default="cpu",
                           help="torch device for VITS (default cpu: ~0.8s per reply and leaves "
                                "the GPU to whisper; use cuda if VRAM is free)")

    @classmethod
    def from_args(cls, args):
        return cls(model=args.vits_model, lang=args.vits_lang,
                   speaker=args.vits_speaker, device=args.vits_device)

    def command(self):
        cmd = [PYTHON, CLI, "--serve", "-m", self.model,
               "-s", str(self.speaker), "--device", self.device]
        if self.model == "trilingual":
            cmd += ["-l", self.lang]
        return cmd

    def describe(self):
        return f"VITS worker on {self.device} ({self.model}/{self.lang}/speaker {self.speaker})"
