"""Chatterbox Turbo (Resemble AI) — the default voice.

350M params, MIT licensed, and able to clone a voice from a single reference
clip with no training step: pass --chatterbox-prompt a clip of 10-20s of
clean single-speaker audio, or omit it to use the model's own default voice.

Runs in the `chatterbox-tts` conda env (requirements/chatterbox.txt) via
chatterbox_cli.py, because its torch/CUDA pins conflict with the `chat` env's.

Standalone debug use (writes wavs, doesn't play them):
    python -m voicepipe speak "hello there"
"""
import os

from ..registry import TTS
from ..subproc import WorkerVoice

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.expanduser("~/anaconda3/envs/chatterbox-tts/bin/python")
CLI = os.path.join(REPO, "chatterbox_cli.py")


@TTS.register("chatterbox")
class ChatterboxVoice(WorkerVoice):
    name = "chatterbox"

    def __init__(self, device="cuda", prompt=None, nano=False, hooks=None):
        super().__init__(hooks=hooks)
        self.device = device
        self.prompt = prompt
        self.nano = nano

    @staticmethod
    def add_arguments(group):
        group.add_argument("--chatterbox-device", default="cuda",
                           help="torch device for Chatterbox (default: cuda)")
        group.add_argument("--chatterbox-prompt", default=None, metavar="CLIP.WAV",
                           help="reference clip to clone a voice from — 10-20s of clean, "
                                "single-speaker audio; omit for the model's own default voice")
        group.add_argument("--chatterbox-nano", action="store_true",
                           help="Nano (110M) instead of Turbo (350M): same voice cloning, "
                                "small enough to run on CPU and hand the GPU to the LLM")

    @classmethod
    def from_args(cls, args):
        return cls(device=args.chatterbox_device, prompt=args.chatterbox_prompt,
                   nano=args.chatterbox_nano)

    def command(self):
        cmd = [PYTHON, CLI, "--serve", "--device", self.device]
        if self.prompt:
            cmd += ["--prompt", self.prompt]
        if self.nano:
            cmd += ["--nano"]
        return cmd

    def describe(self):
        voice = os.path.basename(self.prompt) if self.prompt else "default voice"
        return (f"Chatterbox {'Nano' if self.nano else 'Turbo'} worker "
                f"on {self.device} ({voice})")
