"""Speech-to-text: faster-whisper. Runs in the `chat` conda env.

Standalone debug use:
    conda run -n chat python -m voicepipe.stt some.wav [--model small] [--lang en]
"""
import os
import sys
import tempfile
import wave

from .registry import STT

TMP = tempfile.gettempdir()


def ensure_cuda_libs():
    """ctranslate2 dlopen's libcublas/libcudnn from the nvidia-*-cu12 wheels, but
    only if they're on LD_LIBRARY_PATH at process start. Add them and re-exec
    once. Call this once, early, from any entrypoint that wants GPU whisper
    (chat_loop.py and bridge_server.py both do) — it's a no-op if the libs are
    already reachable or just aren't installed (CPU-only box)."""
    if os.environ.get("_VOICEPIPE_CUDA_REEXEC") or not os.path.isfile(sys.argv[0]):
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
    os.environ["_VOICEPIPE_CUDA_REEXEC"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])


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
    """Load a faster-whisper model, preferring CUDA then falling back to CPU
    when `want_device` is "auto"."""
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


class FasterWhisperSTT:
    """STTBackend: faster-whisper. Loads the model once at construction (see
    load_stt() above for the cuda->cpu fallback)."""

    def __init__(self, model="small", device="auto"):
        self.model = load_stt(model, device)

    def transcribe(self, wav_path, lang):
        return transcribe(self.model, wav_path, lang)


STT.register("faster-whisper")(FasterWhisperSTT)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", help="wav file to transcribe")
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--lang", default="en", help="or 'auto' to detect")
    args = ap.parse_args()
    m = load_stt(args.model, args.device)
    lang = None if args.lang == "auto" else args.lang
    print(transcribe(m, args.wav, lang))
