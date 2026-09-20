"""Kokoro-82M TTS backend.

Kokoro is intentionally loaded in-process: its small ONNX/PyTorch-sized model
is cheap to keep warm, and the backend can be pinned to CPU so Whisper keeps
the GTX 1650.  The default voice is Kokoro's American-English female
``af_bella`` voice.
"""
import os
import shutil
import tempfile

from ..registry import TTS
from ..text import chunks


@TTS.register("kokoro")
class KokoroVoice:
    name = "kokoro"

    def __init__(self, voice="af_bella", language="a", device="cpu"):
        self.voice = voice
        self.language = language
        self.device = device
        self._outdir = tempfile.mkdtemp(prefix="voicepipe-kokoro-")
        # Import lazily so the normal test/CLI environment does not need the
        # optional Kokoro stack merely to list backends.
        from kokoro import KPipeline

        self.pipeline = KPipeline(lang_code=language, device=device)

    @staticmethod
    def add_arguments(group):
        group.add_argument("--kokoro-voice", default="af_bella",
                           help="Kokoro voice pack (default: af_bella)")
        group.add_argument("--kokoro-language", default="a",
                           help="Kokoro language code (a=American English, j=Japanese)")
        group.add_argument("--kokoro-device", default="cpu",
                           help="Kokoro device; cpu avoids competing with Whisper")

    @classmethod
    def from_args(cls, args):
        return cls(voice=args.kokoro_voice, language=args.kokoro_language,
                   device=args.kokoro_device)

    def synth(self, text):
        import numpy as np
        import soundfile as sf

        paths = []
        for index, chunk in enumerate(chunks(text)):
            audio_parts = []
            for result in self.pipeline(chunk, voice=self.voice, split_pattern=r"\n+"):
                if result.audio is None:
                    continue
                audio = result.audio.detach().cpu().numpy()
                audio_parts.append(np.asarray(audio, dtype=np.float32))
            if not audio_parts:
                continue
            path = os.path.join(self._outdir, f"chunk_{index}.wav")
            sf.write(path, np.concatenate(audio_parts), 24000)
            paths.append(path)
        return paths

    def close(self):
        shutil.rmtree(self._outdir, ignore_errors=True)
