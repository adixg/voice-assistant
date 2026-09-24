"""Speech-to-text on CPU via sherpa-onnx: NVIDIA Parakeet TDT and Moonshine.

Both are alternatives to faster-whisper that don't need a GPU, so the STT pod
can stop time-slicing the always-on GTX 1650 with the LLM. Measured on this
laptop's CPU (4 threads, 2026-09-24, a 3.8 s clip, warm):

    parakeet   0.6B, int8, 25 European languages   ~0.25 s   ~815 MB RSS
    moonshine  base, quantized, English only        ~0.12 s   ~370 MB RSS

Models download on first use (see _sherpa.py). Switch with --stt-backend.

Standalone debug use:
    python -m voicepipe transcribe some.wav --stt-backend parakeet
"""
from ..registry import STT
from ._sherpa import model_dir, read_wav

PARAKEET_MODEL = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MOONSHINE_MODEL = "sherpa-onnx-moonshine-base-en-quantized-2026-02-27"
DEFAULT_THREADS = 4


class _SherpaSTT:
    """STTBackend over a sherpa-onnx OfflineRecognizer. Subclasses build it."""

    def __init__(self, recognizer):
        self.recognizer = recognizer
        # Force a real inference at load, so a broken model surfaces at
        # startup rather than on the first utterance (as whisper.py does).
        self._decode(16000, [0.0] * 1600)

    def _decode(self, rate, samples):
        stream = self.recognizer.create_stream()
        stream.accept_waveform(rate, samples)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def transcribe(self, wav_path, lang):
        # `lang` is ignored: Parakeet v3 detects the language itself and
        # Moonshine's English model has only one.
        return self._decode(*read_wav(wav_path))


@STT.register("parakeet")
class ParakeetSTT(_SherpaSTT):
    def __init__(self, model=PARAKEET_MODEL, threads=DEFAULT_THREADS):
        import sherpa_onnx

        path = model_dir(model, "asr-models")
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=f"{path}/encoder.int8.onnx", decoder=f"{path}/decoder.int8.onnx",
            joiner=f"{path}/joiner.int8.onnx", tokens=f"{path}/tokens.txt",
            num_threads=threads, model_type="nemo_transducer")
        super().__init__(recognizer)
        print(f"  parakeet '{model}' on cpu ({threads} threads)")

    @staticmethod
    def add_arguments(group):
        group.add_argument("--parakeet-model", default=PARAKEET_MODEL,
                           help="sherpa-onnx release name or local model directory "
                                f"(default: {PARAKEET_MODEL})")
        group.add_argument("--parakeet-threads", type=int, default=DEFAULT_THREADS,
                           help=f"CPU threads (default: {DEFAULT_THREADS})")

    @classmethod
    def from_args(cls, args):
        return cls(model=args.parakeet_model, threads=args.parakeet_threads)


@STT.register("moonshine")
class MoonshineSTT(_SherpaSTT):
    def __init__(self, model=MOONSHINE_MODEL, threads=DEFAULT_THREADS):
        import sherpa_onnx

        path = model_dir(model, "asr-models")
        recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=f"{path}/encoder_model.ort", decoder=f"{path}/decoder_model_merged.ort",
            tokens=f"{path}/tokens.txt", num_threads=threads)
        super().__init__(recognizer)
        print(f"  moonshine '{model}' on cpu ({threads} threads)")

    @staticmethod
    def add_arguments(group):
        group.add_argument("--moonshine-model", default=MOONSHINE_MODEL,
                           help="sherpa-onnx release name or local model directory "
                                f"(default: {MOONSHINE_MODEL})")
        group.add_argument("--moonshine-threads", type=int, default=DEFAULT_THREADS,
                           help=f"CPU threads (default: {DEFAULT_THREADS})")

    @classmethod
    def from_args(cls, args):
        return cls(model=args.moonshine_model, threads=args.moonshine_threads)
