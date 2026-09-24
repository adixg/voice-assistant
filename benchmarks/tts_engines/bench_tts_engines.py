#!/usr/bin/env python3
"""Time every TTS engine on the same sentences, and measure its memory.

Each engine runs in its own child process, so its peak memory is its own and
nothing it loads leaks into the next one. Per engine: load time, then untimed
warm-ups, then --repetitions passes over every sentence, each timed through
the backend's real synth() (chunking and WAV writing included). Reported:
seconds to make 10 s of speech, x realtime, per-call p50/p95, and peak RSS.

    python benchmarks/tts_engines/bench_tts_engines.py \\
        --engine "Kitten nano=kitten" \\
        --engine "Kitten mini=kitten --kitten-model kitten-mini-en-v0_8" \\
        --engine "Kokoro-ONNX fp32=kokoro-onnx" \\
        --warmups 3 --repetitions 10 --device cpu --host-label arch-ssd \\
        --keep "arch-ssd CPU"

--engine is LABEL=BACKEND followed by that backend's own flags (see
`python -m services.tts.app --help`). Must run where voicepipe and the
backends' packages import: this repo's `chat` env, or inside the tts image
(how the arch-ssd numbers are taken; see benchmarks/tts_engines/README.md).

Raw per-call rows go to --output-dir (gitignored). --keep also writes a small
summary to benchmarks/runs/tts_engines/, meant to be committed; the README's
speed-vs-memory chart (benchmarks/plot_tts_tradeoff.py) is drawn from those.
"""
import argparse
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SENTENCES = ROOT / "benchmarks" / "sentences.txt"


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p / 100
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def parse_engine(spec):
    """'Kitten mini=kitten --kitten-model x' -> ('Kitten mini', 'kitten', ['--kitten-model', 'x'])."""
    label, sep, rest = spec.partition("=")
    parts = shlex.split(rest)
    if not sep or not label.strip() or not parts:
        raise ValueError(f"--engine wants LABEL=BACKEND [flags...], got {spec!r}")
    return label.strip(), parts[0], parts[1:]


def read_sentences(path):
    return [line.strip() for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _status_kib(pid, field):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def _descendants(pid):
    out = []
    for task in Path(f"/proc/{pid}/task").glob("*"):
        try:
            children = (task / "children").read_text().split()
        except OSError:
            continue
        for child in children:
            out += [int(child)] + _descendants(int(child))
    return out


def peak_rss_mib():
    """Peak resident memory of this process plus any worker processes it
    started (the vits/chatterbox backends synthesize in a subprocess)."""
    pids = [os.getpid()] + _descendants(os.getpid())
    return round(sum(_status_kib(p, "VmHWM") for p in pids) / 1024, 1)


def wav_seconds(paths):
    total = 0.0
    for path in paths:
        with wave.open(path, "rb") as w:
            total += w.getnframes() / w.getframerate()
    return total


def run_child(spec):
    """Measure one engine; prints one RESULT line of JSON."""
    sys.path.insert(0, str(ROOT))
    from voicepipe import backends  # noqa: F401 - registers every backend
    from voicepipe.registry import TTS

    ap = argparse.ArgumentParser()
    TTS.add_arguments(ap)
    args = ap.parse_args(spec["flags"])
    started = time.perf_counter()
    voice = TTS.build(spec["backend"], args)
    load_s = time.perf_counter() - started
    calls = []
    try:
        for _ in range(spec["warmups"]):
            voice.synth(spec["sentences"][0])
        for rep in range(spec["repetitions"]):
            for index, sentence in enumerate(spec["sentences"]):
                t0 = time.perf_counter()
                paths = voice.synth(sentence)
                seconds = time.perf_counter() - t0
                calls.append({"rep": rep, "sentence": index, "synth_s": round(seconds, 4),
                              "audio_s": round(wav_seconds(paths), 4)})
        rss = peak_rss_mib()
    finally:
        voice.close()
    print("RESULT " + json.dumps({"label": spec["label"], "backend": spec["backend"],
                                  "options": TTS.options(spec["backend"], args),
                                  "load_s": round(load_s, 2), "rss_peak_mib": rss,
                                  "calls": calls}), flush=True)


def summarize(result):
    calls = result["calls"]
    synth = [c["synth_s"] for c in calls]
    audio_total, synth_total = sum(c["audio_s"] for c in calls), sum(synth)
    per_call = [c["audio_s"] / c["synth_s"] for c in calls if c["synth_s"] > 0]
    x_rt = audio_total / synth_total if synth_total else None
    return {"label": result["label"], "backend": result["backend"], "options": result["options"],
            "n": len(calls), "load_s": result["load_s"], "rss_peak_mib": result["rss_peak_mib"],
            "x_realtime": round(x_rt, 3) if x_rt else None,
            "s_per_10s_speech": round(10 / x_rt, 3) if x_rt else None,
            "x_realtime_p50": round(percentile(per_call, 50), 3) if per_call else None,
            "x_realtime_min": round(min(per_call), 3) if per_call else None,
            "synth_s_p50": percentile(synth, 50), "synth_s_p95": percentile(synth, 95),
            "synth_s_max": max(synth) if synth else None,
            "audio_s_total": round(audio_total, 2)}


def cpu_model():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", action="append", required=False, metavar="LABEL=BACKEND [flags]",
                    help="engine to measure; repeatable")
    ap.add_argument("--sentences-file", default=str(DEFAULT_SENTENCES))
    ap.add_argument("--warmups", type=int, default=3)
    ap.add_argument("--repetitions", type=int, default=10)
    ap.add_argument("--device", default="cpu", help="what the engines ran on, for the record: cpu or gpu")
    ap.add_argument("--host-label", default=platform.node(), help="machine name for the record")
    ap.add_argument("--git-commit", default=None, help="recorded as-is (inside a pod there is no git)")
    ap.add_argument("--output-dir", type=Path, default=ROOT / "benchmarks" / "results" / "tts_engines")
    ap.add_argument("--keep", metavar="LABEL", help="also write a committable summary to --runs-dir")
    ap.add_argument("--runs-dir", type=Path, default=ROOT / "benchmarks" / "runs" / "tts_engines")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.child:
        run_child(json.loads(args.child))
        return 0
    if not args.engine:
        ap.error("give at least one --engine")
    sentences = read_sentences(args.sentences_file)
    results, failures = [], []
    for spec_text in args.engine:
        label, backend, flags = parse_engine(spec_text)
        spec = {"label": label, "backend": backend, "flags": flags, "sentences": sentences,
                "warmups": args.warmups, "repetitions": args.repetitions}
        print(f"== {label} ({backend} {' '.join(flags)})", flush=True)
        proc = subprocess.run([sys.executable, __file__, "--child", json.dumps(spec)],
                              capture_output=True, text=True)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        if proc.returncode or not line:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            print(f"   FAILED (exit {proc.returncode}): {' | '.join(tail)}", flush=True)
            failures.append({"label": label, "backend": backend, "flags": flags,
                             "exit": proc.returncode, "error": tail})
            continue
        result = json.loads(line[len("RESULT "):])
        results.append(result)
        s = summarize(result)
        print(f"   {s['s_per_10s_speech']:.2f} s per 10 s of speech ({s['x_realtime']:.2f}x realtime, "
              f"slowest call {s['x_realtime_min']:.2f}x), peak {s['rss_peak_mib']:.0f} MiB, "
              f"load {s['load_s']:.1f} s, n={s['n']}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    metadata = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "host": args.host_label,
                "device": args.device, "cpu": cpu_model(), "cpus": os.cpu_count(),
                "warmups": args.warmups, "repetitions": args.repetitions, "sentences": sentences,
                "git_commit": args.git_commit, "failures": failures,
                "memory_note": "rss_peak_mib is the engine process's own peak RSS (VmHWM), plus "
                               "any worker processes it started; not a pod's cgroup total"}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw = args.output_dir / f"{stamp}.json"
    raw.write_text(json.dumps({"metadata": metadata, "results": results}, indent=2) + "\n")
    print(f"\nsaved {raw}")
    if args.keep:
        slug = re.sub(r"[^a-z0-9]+", "-", args.keep.lower()).strip("-")[:60] or "run"
        args.runs_dir.mkdir(parents=True, exist_ok=True)
        kept = args.runs_dir / f"{stamp}-{slug}.json"
        kept.write_text(json.dumps({"label": args.keep, "metadata": metadata,
                                    "summary": [summarize(r) for r in results]}, indent=2) + "\n")
        print(f"kept {kept}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
