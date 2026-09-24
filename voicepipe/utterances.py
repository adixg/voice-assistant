"""Keeping the last few utterances the gateway heard, so real Stick audio can be
reviewed and added to the voiceprint (tools/voiceprint_add.py).

Opt-in and bounded: the gateway only does this with --keep-utterances, and only
the newest `max_keep` files survive. The gate's verdict and score go in the file
name, because the interesting clips are the owner's that scored low.
"""
import os
import shutil
import time

DEFAULT_MAX_KEEP = 50


def keep(wav_path, directory, label, score, max_keep=DEFAULT_MAX_KEEP):
    """Copy `wav_path` into `directory` as <time>_<label>_<score>.wav and prune
    the oldest beyond `max_keep`. Returns the new path, or None if it couldn't
    (a kept copy is a convenience and must never break a turn)."""
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = "none" if score is None else f"{score:.3f}"
        dest = os.path.join(directory, f"{stamp}_{label}_{tag}.wav")
        n = 1
        while os.path.exists(dest):  # two turns in one second
            n += 1
            dest = os.path.join(directory, f"{stamp}-{n}_{label}_{tag}.wav")
        shutil.copyfile(wav_path, dest)
        prune(directory, max_keep)
        return dest
    except OSError as e:
        print(f"  (couldn't keep the utterance: {e})", flush=True)
        return None


def prune(directory, max_keep):
    wavs = sorted(f for f in os.listdir(directory) if f.endswith(".wav"))
    for old in wavs[:max(0, len(wavs) - max_keep)]:
        try:
            os.unlink(os.path.join(directory, old))
        except OSError:
            pass
