"""Download and convert microWakeWord's augmentation and negative data.

Mirrors kahrendt/microWakeWord's basic_training_notebook.ipynb (the data cells).
The downloaded data carries mixed licenses; models trained with it are for
non-commercial personal use only.
"""
import os
import subprocess
import zipfile
from pathlib import Path

import datasets
import numpy as np
import scipy.io.wavfile
from tqdm import tqdm

ROOT = Path(__file__).parent
HF = "https://huggingface.co/datasets"


def fetch(url, dest):
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["curl", "-sSL", "--retry", "5", "-C", "-", "-o", str(dest), url], check=True)
    return dest


def to_wavs(files, out_dir):
    """16 kHz mono 16-bit wavs via ffmpeg (handles flac and mp3 alike)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    for f in tqdm(list(files), desc=out_dir.name):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(f), "-ac", "1", "-ar", "16000",
                        "-sample_fmt", "s16", str(out_dir / (Path(f).stem + ".wav"))], check=False)


def decode_16k(audio):
    """A datasets Audio(decode=False) value -> float mono samples at 16 kHz."""
    import io
    from math import gcd

    import soundfile as sf
    from scipy.signal import resample_poly
    data, rate = sf.read(io.BytesIO(audio["bytes"]) if audio.get("bytes") else audio["path"], dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if rate != 16000:
        g = gcd(rate, 16000)
        data = resample_poly(data, 16000 // g, rate // g)
    return data


def main():
    os.chdir(ROOT)
    # Room impulse responses
    if not Path("mit_rirs").exists():
        Path("mit_rirs").mkdir()
        from huggingface_hub import snapshot_download
        repo = snapshot_download("davidscripka/MIT_environmental_impulse_responses", repo_type="dataset",
                                 allow_patterns=["16khz/*.wav"])
        for f in Path(repo, "16khz").glob("*.wav"):
            samples = np.clip(decode_16k({"path": str(f)}), -1, 1)
            scipy.io.wavfile.write(Path("mit_rirs") / f.name, 16000, (samples * 32767).astype(np.int16))

    # Background: one AudioSet shard (the dataset is now parquet, not .tar)
    if not Path("audioset_16k").exists():
        import pyarrow.parquet as pq
        shard = fetch(f"{HF}/agkphysics/AudioSet/resolve/main/data/bal_train/09.parquet", "audioset/09.parquet")
        raw = Path("audioset/raw")
        raw.mkdir(parents=True, exist_ok=True)
        table = pq.read_table(shard, columns=["audio"])
        for i, audio in enumerate(table.column("audio").to_pylist()):
            name = Path(audio.get("path") or f"clip_{i}.flac").name
            (raw / name).write_bytes(audio["bytes"])
        to_wavs(raw.glob("*"), "audioset_16k")

    # Background: Free Music Archive extra-small
    if not Path("fma_16k").exists():
        z = fetch(f"{HF}/mchl914/fma_xsmall/resolve/main/fma_xs.zip", "fma/fma_xs.zip")
        zipfile.ZipFile(z).extractall("fma")
        to_wavs(Path("fma/fma_small").glob("**/*.mp3"), "fma_16k")

    # Negative spectrogram features, precomputed by the microWakeWord author
    for name in ["dinner_party", "dinner_party_eval", "no_speech", "speech"]:
        if Path("negative_datasets", name).exists():
            continue
        z = fetch(f"{HF}/kahrendt/microwakeword/resolve/main/{name}.zip", f"negative_datasets/{name}.zip")
        zipfile.ZipFile(z).extractall("negative_datasets")
        z.unlink()
    print("data ready")


if __name__ == "__main__":
    main()
