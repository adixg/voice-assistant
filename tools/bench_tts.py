#!/usr/bin/env python3
"""Continuously benchmarks every registered TTS backend against the same
sentences, side by side — for judging one voice engine against another under
real, current conditions: whatever else is running on the GPU/CPU right now
(a live bridge_server.py, say), not a synthetic idle-machine number.

Starts its own worker per backend, separate from any running bridge's, so the
GPU is shared rather than stolen; check `nvidia-smi` first if you're
benchmarking while the Stick is in use and want to know the headroom.

Run in the `chat` env — this only manages the backends' worker subprocesses,
and has no heavy imports of its own:

    conda activate chat
    python tools/bench_tts.py                       # loops forever, ~60s between rounds
    python tools/bench_tts.py --interval 30          # faster loop
    python tools/bench_tts.py --once                 # single round, then exit
    python tools/bench_tts.py --backends chatterbox  # just one engine

Every backend's own flags work here too (--chatterbox-prompt to benchmark a
cloned voice, --vits-device cuda to compare against GPU VITS, ...); see --help.

Appends every round to bench_tts_log.csv at the repo root — timestamp,
backend, sentence index, latency, audio duration, realtime factor — so trends
(thermal throttling, GPU contention, a slower reply after an hour of talking)
are there to chart later without re-running anything.
"""
import argparse
import csv
import os
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from voicepipe import backends  # noqa: E402,F401 - registers every backend
from voicepipe.registry import TTS  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(REPO, "bench_tts_log.csv")
LOG_HEADER = ["timestamp", "backend", "sentence_idx", "latency_s", "audio_s", "rtf"]

# Short, medium and longer — roughly the spread of a real reply (main.cpp's
# caption area keeps them short), so the comparison reflects what the Stick
# actually says rather than an arbitrary string.
SENTENCES = [
    "Hey, I'm here.",
    "That's a good question, let me think about it for a second.",
    "Sure thing, I'll go ahead and take care of that for you right away.",
]


def gpu_stats():
    """A one-line GPU utilisation/VRAM snapshot, or a note if there's no nvidia-smi."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        util, used, total = (x.strip() for x in out.split(","))
        return f"GPU {util}%  VRAM {used}/{total} MiB"
    except Exception:  # noqa: BLE001 - purely informational, never fatal
        return "GPU stats unavailable"


def wav_duration(path):
    """Seconds of audio in a wav, via the stdlib (the `chat` env has no soundfile)."""
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def bench_once(voice, text):
    """Synthesize one sentence; return (wall-clock seconds, audio seconds)."""
    t0 = time.time()
    paths = voice.synth(text)
    elapsed = time.time() - t0
    return elapsed, sum(wav_duration(p) for p in paths)


def run_round(voices, writer):
    """One pass over every backend and sentence; returns per-backend results."""
    rows = {}
    for name, voice in voices.items():
        results = []
        for i, text in enumerate(SENTENCES):
            elapsed, audio_s = bench_once(voice, text)
            results.append((elapsed, audio_s, audio_s / elapsed if elapsed else 0.0))
            writer.writerow([datetime.now(timezone.utc).isoformat(), name, i,
                             f"{elapsed:.3f}", f"{audio_s:.3f}", f"{results[-1][2]:.2f}"])
        rows[name] = results
    return rows


def print_summary(round_n, rows):
    """The per-round comparison table.

    Everything here is flushed explicitly: when stdout is a file rather than a
    terminal it is fully buffered, so an unflushed summary can sit invisible
    for hours while the CSV beside it fills up normally.
    """
    print(f"\n=== round {round_n} — {datetime.now().strftime('%H:%M:%S')} — {gpu_stats()} ===", flush=True)
    print(f"{'backend':<12} {'avg latency':>12} {'avg audio':>10} {'avg RTF':>9}", flush=True)
    for name, results in rows.items():
        n = len(results)
        print(f"{name:<12} {sum(r[0] for r in results) / n:>11.2f}s "
              f"{sum(r[1] for r in results) / n:>9.2f}s "
              f"{sum(r[2] for r in results) / n:>8.2f}x", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=60, help="seconds between rounds (default: 60)")
    ap.add_argument("--once", action="store_true", help="run a single round and exit")
    ap.add_argument("--backends", nargs="+", default=TTS.names(), choices=TTS.names(),
                    help="which backends to compare (default: all registered)")
    TTS.add_arguments(ap)  # each backend contributes its own options
    args = ap.parse_args()

    voices = {}
    try:
        for name in args.backends:
            voice = TTS.build(name, args)
            voice.start()
            voices[name] = voice

        new_log = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="") as log:
            writer = csv.writer(log)
            if new_log:
                writer.writerow(LOG_HEADER)

            round_n = 0
            while True:
                round_n += 1
                rows = run_round(voices, writer)
                log.flush()
                print_summary(round_n, rows)
                if args.once:
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        for voice in voices.values():
            voice.close()


if __name__ == "__main__":
    main()
