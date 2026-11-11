#!/usr/bin/env python3
"""Draw the README's two TTS charts, light and dark, standard library only.

    python benchmarks/plot_tts_tradeoff.py

writes assets/charts/tts-cpu-speed-vs-ram-{light,dark}.svg (engines on the
home server's CPU, by process RAM) and tts-gpu-speed-vs-vram-{light,dark}.svg
(engines on a GPU, by VRAM). RAM and VRAM are different budgets, so they get
different charts.

Speed is seconds to make 10 s of speech (10 / x realtime) from the kept run
summaries in benchmarks/runs/tts_engines/ (bench_tts_engines.py --keep), the
newest run per engine. GPU memory comes from *-tts-vram.json in the same
folder. Nothing is typed in here; re-run the benchmark with --keep, then this.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chart_svg import THEMES, mib, scatter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "benchmarks" / "runs" / "tts_engines"

# Engines renamed between runs; the newest run under either name wins.
ALIASES = {"Kitten nano 1.6x (deployed)": "Kitten nano 1.6x (live)",
           "Kokoro PyTorch GPU": "Kokoro PyTorch"}
# Measured but not drawn: plain Kitten nano is the live engine at normal
# speed and would sit on top of the live point.
HIDE = {"Kitten nano"}
CPU_PLACE = {"VITS": (12, 4, "start")}
# (label, card) -> label placement on the GPU chart
GPU_PLACE = {("VITS", "gtx1650"): (4, -26, "end"), ("Kokoro PyTorch", "gtx1650"): (-4, -26, "start"),
             ("Chatterbox Nano", "rtx4060"): (-12, 16, "end"), ("Chatterbox Turbo", "rtx4060"): (4, -26, "end"),
             ("Chatterbox Nano", "gtx1650"): (-12, 4, "end"), ("Chatterbox Turbo", "gtx1650"): (-12, 4, "end")}
CARD_NAMES = {"gtx1650": "GTX 1650", "rtx4060": "RTX 4060"}


def run_kind(path, run):
    """cpu, gtx1650 or rtx4060: the hardware a run measured."""
    if run.get("metadata", {}).get("device", "cpu") == "cpu":
        return "cpu"
    return "rtx4060" if "rtx4060" in path.name else "gtx1650"


def load_speed(runs=RUNS):
    """(label, kind) -> summary entry, from the newest run that measured it."""
    newest = {}
    for path in sorted(runs.glob("*.json")):  # names start with a UTC stamp
        run = json.loads(path.read_text())
        for entry in run.get("summary", []):
            if entry.get("x_realtime"):
                newest[(ALIASES.get(entry["label"], entry["label"]), run_kind(path, run))] = entry
    return newest


def load_vram(runs=RUNS):
    """(label, card) -> VRAM entry."""
    vram = {}
    for path in sorted(runs.glob("*-tts-vram.json")):
        for e in json.loads(path.read_text())["engines"]:
            vram[(e["label"], e["card"])] = e
    return vram


def slowest(e):
    """Bar up to the slowest single call, the only per-call spread the kept
    summaries have (x_realtime_min)."""
    return {"yhi": 10 / e["x_realtime_min"]} if e.get("x_realtime_min") else {}


def cpu_chart(theme, speed, _vram):
    pts = []
    for (label, kind), e in sorted(speed.items()):
        if kind != "cpu" or label in HIDE or not e.get("rss_peak_mib"):
            continue
        sec = 10 / e["x_realtime"]
        pts.append({"x": e["rss_peak_mib"], "y": sec, "kind": "cpu", "label": label, **slowest(e),
                    "value": f"{sec:.1f} s · {mib(e['rss_peak_mib'])} · n={e['n']}",
                    "place": CPU_PLACE.get(label, (12, 4, "start"))})
    return scatter(theme, title="TTS on the home server's CPU: seconds per 10 s of speech by RAM",
                   legend=[("cpu", "Intel i5-10300H, 8 threads")], points=pts,
                   x=(200, 3000, True), y=(0, 16, False),
                   xticks=[(200, "200 MiB"), (500, "500 MiB"), (1024, "1 GB"), (2048, "2 GB")],
                   yticks=[(v, f"{v} s") for v in (0, 4, 8, 12, 16)],
                   xlabel="peak RAM of the engine process (log scale)",
                   note="dot: all calls together · bar: up to the slowest call · below 10 s is faster than realtime")


def gpu_chart(theme, speed, vram):
    pts = []
    for (label, card), v in sorted(vram.items(), key=lambda kv: kv[1]["vram_peak_mib"]):
        e = speed.get((label, card))
        if not e:
            continue
        sec = 10 / e["x_realtime"]
        mem = ("≥" if v.get("lower_bound") else "") + mib(v["vram_peak_mib"])
        both = sum(1 for (other, _c) in vram if other == label) > 1
        pts.append({"x": v["vram_peak_mib"], "y": sec, "kind": card, **slowest(e),
                    "label": label + (f" ({CARD_NAMES[card]})" if both else ""),
                    "value": f"{sec:.1f} s · {mem} · n={e['n']}",
                    "place": GPU_PLACE.get((label, card), (12, 4, "start"))})
    return scatter(theme, title="TTS on a GPU: seconds per 10 s of speech by VRAM",
                   legend=[("gtx1650", "GTX 1650, 4 GB"), ("rtx4060", "RTX 4060, 8 GB")], points=pts,
                   x=(200, 10000, True), y=(0, 8.5, False),
                   xticks=[(200, "200 MiB"), (500, "500 MiB"), (1024, "1 GB"), (2048, "2 GB"),
                           (4096, "4 GB"), (8192, "8 GB")],
                   yticks=[(v, f"{v} s") for v in range(0, 9)],
                   xlabel="VRAM in use on the card during the run (log scale)",
                   note="dot: all calls together · bar: up to the slowest call",
                   limits=[(4096, "GTX 1650 limit"), (8192, "RTX 4060 limit")])


def main():
    speed, vram = load_speed(), load_vram()
    root = ROOT / "assets" / "charts"
    root.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        for stem, draw in (("tts-cpu-speed-vs-ram", cpu_chart), ("tts-gpu-speed-vs-vram", gpu_chart)):
            path = root / f"{stem}-{theme}.svg"
            path.write_text(draw(theme, speed, vram))
            print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
