"""Speech-to-text via faster-whisper. Runs in the `chat` env, in-process —
unlike the TTS backends it has no conflicting dependencies to isolate.

Standalone debug use:
    python -m voicepipe transcribe some.wav --whisper-model small
"""
import os
import struct
import tempfile
import wave

from ..registry import STT

SIZES = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]


def _probe_wav():
    """A tenth of a second of silence, for forcing a real inference at load."""
    path = os.path.join(tempfile.gettempdir(), "stt_probe.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<1600h", *([0] * 1600)))
    return path


def load_model(name, want_device):
    """Load a faster-whisper model, preferring CUDA and falling back to CPU
    when `want_device` is "auto".

    ctranslate2 only touches its CUDA libraries on first use, so a broken GPU
    setup would otherwise surface on the first real utterance rather than at
    startup — hence the silent-wav inference before returning.
    """
    from faster_whisper import WhisperModel

    attempts = []
    if want_device in ("auto", "cuda"):
        attempts.append(("cuda", "float16"))
    if want_device in ("auto", "cpu"):
        attempts.append(("cpu", "int8"))

    probe = _probe_wav()
    last_error = None
    for device, compute in attempts:
        try:
            model = WhisperModel(name, device=device, compute_type=compute)
            list(model.transcribe(probe, without_timestamps=True)[0])
            print(f"  faster-whisper '{name}' on {device} ({compute})")
            return model
        except Exception as e:  # noqa: BLE001 - missing CUDA libs, no GPU, ...
            last_error = e
            if want_device == "auto" and device == "cuda":
                print(f"  (whisper cuda unavailable: {str(e).splitlines()[0]}; falling back to cpu)")
    raise last_error


@STT.register("faster-whisper")
class FasterWhisperSTT:
    """STTBackend: loads the model at construction (see load_model above for
    the cuda -> cpu fallback)."""

    def __init__(self, model="small", device="auto"):
        self.model = load_model(model, device)

    @staticmethod
    def add_arguments(group):
        group.add_argument("--whisper-model", default="small", choices=SIZES,
                           help="faster-whisper model size (default: small)")
        group.add_argument("--whisper-device", default="auto", choices=["auto", "cpu", "cuda"],
                           help="where to run whisper (default: auto, cuda then cpu)")

    @classmethod
    def from_args(cls, args):
        return cls(model=args.whisper_model, device=args.whisper_device)

    def transcribe(self, wav_path, lang):
        segments, _ = self.model.transcribe(wav_path, language=lang or None, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
