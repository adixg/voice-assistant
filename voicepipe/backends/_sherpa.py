"""Shared plumbing for the sherpa-onnx backends (sherpa_stt.py, sherpa_tts.py).

sherpa-onnx is one small ONNX runtime package that serves many speech models
on CPU, so a new model is a catalogue entry here rather than a new dependency
stack. Its pre-converted models ship as tarballs on the project's GitHub
releases; `model_dir()` downloads and unpacks one on first use.

Models land in ``$SHERPA_MODELS_DIR`` (default ``~/.cache/sherpa-onnx``). In
the cluster that is a hostPath, so a pod restart doesn't re-download them.

A module starting with "_" is skipped by backend discovery, so this registers
nothing itself.
"""
import os
import shutil
import tarfile
import tempfile
import urllib.request
import wave

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


def models_root():
    return os.environ.get("SHERPA_MODELS_DIR",
                          os.path.join(os.path.expanduser("~"), ".cache", "sherpa-onnx"))


def model_dir(name, release, root=None):
    """Path to the unpacked model `name`, downloading it first if needed.

    `name` may also be an existing directory, to use a model you converted or
    downloaded yourself. `release` is the GitHub release tag it lives under
    ("asr-models" or "tts-models"). The tarball is unpacked into a temporary
    directory and renamed into place, so an interrupted download never leaves
    a half-extracted model that looks complete.
    """
    if os.path.isdir(name):
        return name
    root = root or models_root()
    target = os.path.join(root, name)
    if os.path.isdir(target):
        return target
    os.makedirs(root, exist_ok=True)
    url = f"{RELEASES}/{release}/{name}.tar.bz2"
    print(f"  downloading sherpa-onnx model {name} ...")
    staging = tempfile.mkdtemp(prefix=f".{name}-", dir=root)
    try:
        with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed https host
            with tarfile.open(fileobj=response, mode="r|bz2") as archive:
                archive.extractall(staging, filter="data")
        unpacked = os.path.join(staging, name)
        os.rename(unpacked if os.path.isdir(unpacked) else staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def read_wav(path):
    """(sample_rate, float32 mono samples in [-1, 1]) from a PCM wav file.

    sherpa-onnx takes raw samples and resamples internally, so any rate works;
    multi-channel input is averaged down to mono.
    """
    import numpy as np

    with wave.open(path, "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif width in (2, 4):
        dtype = np.int16 if width == 2 else np.int32
        samples = np.frombuffer(raw, dtype=dtype).astype(np.float32) / np.iinfo(dtype).max
    else:
        raise ValueError(f"unsupported wav sample width: {width} bytes")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return rate, samples


def write_wav(path, samples, rate):
    """Write float samples in [-1, 1] as 16-bit mono PCM."""
    import numpy as np

    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
