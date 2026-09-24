#!/usr/bin/env python3
"""Add real Stick utterances to the speaker voiceprint, without re-enrolling.

The gateway keeps its newest utterances (--keep-utterances), named
<time>_<verdict>_<score>.wav. This scores each one against the current
voiceprint, appends the ones you pick, and pushes the result to the cluster:

    tools/voiceprint_add.py list                  # what was heard, and how it scores
    tools/voiceprint_add.py add 20260924-101500_accepted_0.610.wav ...
    tools/voiceprint_add.py apply                 # update the Secret, restart the gateway

Appending never removes an existing sample (a backup of voiceprint.json is kept).
The gate scores best-of over the samples, so ONE sample that is not you lets
that voice in: `add` refuses clips that score below --min-score against your
current voiceprint, or that are under two seconds, unless --force.

Run it with the project's Python (needs numpy, onnxruntime, kaldi-native-fbank).
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from voicepipe.backends.wespeaker import WeSpeakerSV  # noqa: E402
from voicepipe.speaker import Voiceprint, wav_seconds  # noqa: E402

DEFAULT_DIR = "/var/lib/aicompanion/utterances"
MIN_SECONDS = 2.0     # below this the embedding is unreliable (see wespeaker.py)
MIN_SCORE = 0.4       # an owner clip scores 0.5+; strangers 0.07-0.28


def clips(directory):
    try:
        return sorted(f for f in os.listdir(directory) if f.endswith(".wav"))
    except OSError as e:
        sys.exit(f"can't read {directory}: {e}\n"
                 "the gateway needs --keep-utterances (see deploy/kubernetes/gateway.yaml)")


def gate_tag(name):
    """(verdict, score) the gateway recorded in the file name."""
    parts = name[:-4].split("_")
    return (parts[1] if len(parts) > 1 else "?"), (parts[2] if len(parts) > 2 else "?")


def load():
    voiceprint = Voiceprint.load()
    backend = WeSpeakerSV()
    if voiceprint.backend and voiceprint.backend != backend.name:
        sys.exit(f"voiceprint is from {voiceprint.backend!r}, this tool embeds with {backend.name!r}")
    return voiceprint, backend


def cmd_list(args):
    voiceprint, backend = load()
    names = clips(args.dir)
    if not names:
        print(f"no utterances in {args.dir} yet: talk to the Stick with --keep-utterances on")
        return
    print(f"{len(voiceprint)} samples enrolled. Clips in {args.dir}:\n")
    print(f"  {'clip':44} {'secs':>5} {'gate said':>18} {'vs voiceprint':>14}")
    for name in names:
        path = os.path.join(args.dir, name)
        secs = wav_seconds(path) or 0
        verdict, gscore = gate_tag(name)
        score = voiceprint.score(backend.embed(path)) if secs >= 0.5 else None
        note = "" if secs >= MIN_SECONDS else "  (too short to add)"
        print(f"  {name:44} {secs:5.1f} {verdict + ' ' + gscore:>18} "
              f"{'-' if score is None else f'{score:.3f}':>14}{note}")


def cmd_add(args):
    voiceprint, backend = load()
    added = 0
    for name in args.clips:
        path = name if os.path.isabs(name) else os.path.join(args.dir, name)
        if not os.path.exists(path):
            print(f"  skip {name}: not found")
            continue
        secs = wav_seconds(path) or 0
        embedding = backend.embed(path)
        score = voiceprint.score(embedding)
        problems = []
        if secs < MIN_SECONDS:
            problems.append(f"only {secs:.1f}s (under {MIN_SECONDS}s)")
        if score is not None and score < args.min_score:
            problems.append(f"scores {score:.3f} against your voiceprint (under {args.min_score}): "
                            "this may not be you")
        if problems and not args.force:
            print(f"  REFUSED {name}: {'; '.join(problems)}  (--force to add anyway)")
            continue
        voiceprint.add(embedding, wav_path=path)
        added += 1
        print(f"  added {name}  ({secs:.1f}s, scored {score:.3f} before adding)")
    if not added:
        print("nothing added")
        return
    if os.path.exists(voiceprint.path):
        backup = f"{voiceprint.path}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copyfile(voiceprint.path, backup)
        print(f"  backup: {backup}")
    voiceprint.save()
    print(f"\n{len(voiceprint)} samples now. Run `tools/voiceprint_add.py apply` to put them on the cluster.")


def cmd_apply(args):
    memory = os.path.join(REPO, "memory")
    create = ["kubectl", "-n", "aicompanion", "create", "secret", "generic", "aicompanion-personal-data",
              f"--from-file=about-me.md={memory}/about-me.md",
              f"--from-file=voiceprint.json={memory}/voiceprint.json",
              "--dry-run=client", "-o", "yaml"]
    if not args.yes:
        print("would update Secret aicompanion-personal-data from memory/, then restart the gateway "
              "(the phone's WebSocket drops while it does). Re-run with --yes.")
        return
    env = {**os.environ, "KUBECONFIG": os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))}
    manifest = subprocess.run(create, check=True, capture_output=True, text=True, env=env).stdout
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, check=True, text=True, env=env)
    subprocess.run(["kubectl", "-n", "aicompanion", "rollout", "restart", "deploy/gateway"], check=True, env=env)
    subprocess.run(["kubectl", "-n", "aicompanion", "rollout", "status", "deploy/gateway"], check=True, env=env)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=DEFAULT_DIR, help=f"where the gateway keeps utterances (default: {DEFAULT_DIR})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show kept clips and how they score").set_defaults(fn=cmd_list)
    add = sub.add_parser("add", help="append clips to the voiceprint")
    add.add_argument("clips", nargs="+")
    add.add_argument("--min-score", type=float, default=MIN_SCORE)
    add.add_argument("--force", action="store_true", help="add even a short or low-scoring clip")
    add.set_defaults(fn=cmd_add)
    ap_apply = sub.add_parser("apply", help="update the cluster Secret and restart the gateway")
    ap_apply.add_argument("--yes", action="store_true")
    ap_apply.set_defaults(fn=cmd_apply)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
