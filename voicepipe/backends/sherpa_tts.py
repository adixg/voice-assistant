"""Text-to-speech on CPU via sherpa-onnx: Kokoro (ONNX) and KittenTTS.

`kokoro-onnx` is the same Kokoro-82M v1.0 model and `af_bella` voice as the
`kokoro` backend, int8-quantized and run by onnxruntime instead of PyTorch.
The PyTorch one grew to 2.8 GiB and was OOM-killed on the 7 GiB home server;
this one stays in the hundreds of MiB. `kitten` is smaller still, English
only, with 8 preset voices, for when memory matters more than polish.

Measured on this laptop's CPU (4 threads, 2026-09-24, warm):

    kokoro-onnx  ~2.6 s for 2.7 s of speech   ~360 MB RSS
    kitten       ~1.9 s for 4.3 s of speech   ~250-350 MB RSS (mini)

Models download on first use (see _sherpa.py). Switch with --tts-backend.

Standalone debug use:
    python -m voicepipe speak "hello there" --tts-backend kitten --kitten-voice Luna
"""
import os
import shutil
import tempfile

from ..registry import TTS
from ..text import chunks
from ._sherpa import model_dir, write_wav

DEFAULT_THREADS = 4

KOKORO_MODEL = "kokoro-int8-multi-lang-v1_0"
# Speaker ids in that model's voices.bin, from sherpa-onnx's
# scripts/kokoro/v1.0/generate_voices_bin.py. The prefix is language + sex:
# a=American, b=British, e=Spanish, f=French, h=Hindi, i=Italian, j=Japanese,
# p=Portuguese, z=Mandarin; f=female, m=male.
KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole",
    "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric",
    "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice",
    "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "ff_siwis", "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola", "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro",
    "jm_kumo", "pf_dora", "pm_alex", "pm_santa", "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao",
    "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang", "em_santa",
]

KITTEN_MODEL = "kitten-mini-en-v0_8"
# sherpa-onnx's voices.bin order (scripts/kitten-tts/v0_8/generate_voices_bin.py),
# named with KittenML's own aliases from the model's config.json.
KITTEN_VOICES = ["Jasper", "Bella", "Bruno", "Luna", "Hugo", "Rosie", "Leo", "Kiki"]


def speaker_id(voice, names):
    """A voice name (case-insensitive) or a numeric id -> the model's speaker id."""
    lowered = [n.lower() for n in names]
    if str(voice).lower() in lowered:
        return lowered.index(str(voice).lower())
    if str(voice).isdigit() and int(voice) < len(names):
        return int(voice)
    raise ValueError(f"unknown voice {voice!r}; available: {', '.join(names)}")


def _first(path, *names):
    """The first of `names` present in model directory `path`."""
    for name in names:
        if os.path.exists(os.path.join(path, name)):
            return os.path.join(path, name)
    raise FileNotFoundError(f"none of {names} in {path}")


class _SherpaTTS:
    """TTSBackend over a sherpa-onnx OfflineTts. Subclasses build the config."""

    def __init__(self, model_config, sid, speed):
        import sherpa_onnx

        self.tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=model_config))
        self.sid = sid
        self.speed = speed
        self._outdir = tempfile.mkdtemp(prefix=f"voicepipe-{self.name}-")

    def synth(self, text):
        paths = []
        for index, chunk in enumerate(chunks(text)):
            audio = self.tts.generate(chunk, sid=self.sid, speed=self.speed)
            if len(audio.samples) == 0:
                continue
            path = os.path.join(self._outdir, f"chunk_{index}.wav")
            write_wav(path, audio.samples, audio.sample_rate)
            paths.append(path)
        return paths

    def close(self):
        shutil.rmtree(self._outdir, ignore_errors=True)


@TTS.register("kokoro-onnx")
class KokoroOnnxVoice(_SherpaTTS):
    name = "kokoro-onnx"

    def __init__(self, voice="af_bella", model=KOKORO_MODEL, speed=1.0, threads=DEFAULT_THREADS):
        import sherpa_onnx

        path = model_dir(model, "tts-models")
        lexicon = os.path.join(path, "lexicon-us-en.txt")
        config = sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=_first(path, "model.int8.onnx", "model.onnx"),
                voices=f"{path}/voices.bin", tokens=f"{path}/tokens.txt",
                lexicon=lexicon if os.path.exists(lexicon) else "",
                data_dir=f"{path}/espeak-ng-data", lang="en-us"),
            num_threads=threads)
        super().__init__(config, speaker_id(voice, KOKORO_VOICES), speed)
        print(f"  kokoro-onnx '{model}' voice {voice} on cpu ({threads} threads)")

    @staticmethod
    def add_arguments(group):
        group.add_argument("--kokoro-onnx-voice", default="af_bella",
                           help="voice name or speaker id (default: af_bella)")
        group.add_argument("--kokoro-onnx-model", default=KOKORO_MODEL,
                           help=f"sherpa-onnx release name or local directory (default: {KOKORO_MODEL})")
        group.add_argument("--kokoro-onnx-speed", type=float, default=1.0,
                           help="speaking rate; above 1 is faster (default: 1.0)")
        group.add_argument("--kokoro-onnx-threads", type=int, default=DEFAULT_THREADS,
                           help=f"CPU threads (default: {DEFAULT_THREADS})")

    @classmethod
    def from_args(cls, args):
        return cls(voice=args.kokoro_onnx_voice, model=args.kokoro_onnx_model,
                   speed=args.kokoro_onnx_speed, threads=args.kokoro_onnx_threads)


@TTS.register("kitten")
class KittenVoice(_SherpaTTS):
    name = "kitten"

    def __init__(self, voice="Bella", model=KITTEN_MODEL, speed=1.0, threads=DEFAULT_THREADS):
        import sherpa_onnx

        path = model_dir(model, "tts-models")
        config = sherpa_onnx.OfflineTtsModelConfig(
            kitten=sherpa_onnx.OfflineTtsKittenModelConfig(
                model=_first(path, "model.onnx", "model.int8.onnx", "model.fp16.onnx",
                             "model.fp32.onnx"),
                voices=f"{path}/voices.bin", tokens=f"{path}/tokens.txt",
                data_dir=f"{path}/espeak-ng-data"),
            num_threads=threads)
        super().__init__(config, speaker_id(voice, KITTEN_VOICES), speed)
        print(f"  kitten '{model}' voice {voice} on cpu ({threads} threads)")

    @staticmethod
    def add_arguments(group):
        group.add_argument("--kitten-voice", default="Bella",
                           help=f"one of {', '.join(KITTEN_VOICES)}, or a speaker id (default: Bella)")
        group.add_argument("--kitten-model", default=KITTEN_MODEL,
                           help="sherpa-onnx release name or local directory, e.g. the smaller "
                                f"kitten-nano-en-v0_8-int8 (default: {KITTEN_MODEL})")
        group.add_argument("--kitten-speed", type=float, default=1.0,
                           help="speaking rate; above 1 is faster (default: 1.0)")
        group.add_argument("--kitten-threads", type=int, default=DEFAULT_THREADS,
                           help=f"CPU threads (default: {DEFAULT_THREADS})")

    @classmethod
    def from_args(cls, args):
        return cls(voice=args.kitten_voice, model=args.kitten_model,
                   speed=args.kitten_speed, threads=args.kitten_threads)
