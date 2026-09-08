#!/usr/bin/env python3
"""Score wav files against the enrolled voiceprint, to pick a threshold.

The default threshold is a starting point, not a measurement of *your* voice
in *your* room on *this* microphone. Run this on a few clips of yourself and a
few of someone else, then set --speaker-threshold between the two clusters:

    conda activate chat
    python tools/speaker_check.py me1.wav me2.wav other1.wav

With no arguments it instead reports how the enrolled samples score against
each other, which is the floor a real utterance of yours has to clear — if
those are spread wide, the enrollment clips were inconsistent and re-recording
them will do more good than moving the threshold.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voicepipe.backends.wespeaker import WeSpeakerSV  # noqa: E402
from voicepipe.speaker import DEFAULT_THRESHOLD, Voiceprint  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wavs", nargs="*", help="clips to score against the voiceprint")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    voiceprint = Voiceprint.load()
    if not voiceprint:
        sys.exit(f"no voiceprint at {voiceprint.path} — enrol first:\n"
                 f"    python bridge_server.py --enroll")
    sv = WeSpeakerSV(model=args.model)
    print(f"  voiceprint: {len(voiceprint)} samples, threshold {args.threshold}\n")

    if not args.wavs:
        print("  how consistent the enrolled samples are with each other:")
        for i, sample in enumerate(voiceprint.samples):
            others = [s for j, s in enumerate(voiceprint.samples) if j != i]
            if not others:
                print("  (only one sample — enrol a few more)")
                break
            best = max(sum(a * b for a, b in zip(sample, o)) for o in others)
            print(f"    sample {i + 1}: best match against the others {best:.3f}")
        print("\n  A real utterance of yours should score near these. Anything much\n"
              "  lower and the threshold will reject you.")
        return

    for path in args.wavs:
        try:
            score = voiceprint.score(sv.embed(path))
        except Exception as e:  # noqa: BLE001
            print(f"  {os.path.basename(path):30} failed: {e}")
            continue
        verdict = "ACCEPTED" if score >= args.threshold else "rejected"
        print(f"  {os.path.basename(path):30} {score:.3f}  -> {verdict}")


if __name__ == "__main__":
    main()
