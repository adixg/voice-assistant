#!/usr/bin/env python3
"""Draw the README's two STT charts, light and dark, standard library only.

    python benchmarks/plot_stt.py

writes assets/charts/stt-cpu-speed-vs-ram-{light,dark}.svg (models on the home
server's CPU, by RAM) and stt-gpu-speed-vs-vram-{light,dark}.svg (models on
the GTX 1650, by VRAM). RAM and VRAM are different budgets, so they get
different charts.

Speed is the median time to transcribe one request (stt p50) from the kept
bench_turn summaries in benchmarks/runs/pipeline/ named
stt-<model>-<hardware>.json, newest run per name. Memory comes from
*-stt-memory.json in the same folder (sampled pod working set and VRAM).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chart_svg import THEMES, mib, scatter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "benchmarks" / "runs" / "pipeline"

NAMES = {"moonshine": "Moonshine base", "moonshine-tiny": "Moonshine tiny", "parakeet": "Parakeet",
         "whisper-tiny": "Whisper tiny", "whisper-base": "Whisper base", "whisper-small": "Whisper small",
         "whisper-medium": "Whisper medium", "whisper-large-v2": "Whisper large-v2",
         "whisper-large-v3": "Whisper large-v3"}
LIVE = ("moonshine", "cpu")
GTX1650_MIB = 4096

# Label placement (dx, dy, anchor) per model; None = no label of its own.
CPU_PLACE = {"moonshine-tiny": (8, -24, "start"), "moonshine": (10, 22, "start"),
             "parakeet": (10, 22, "start"), "whisper-tiny": (12, 16, "start"),
             "whisper-base": (-12, -16, "end"), "whisper-small": (12, 4, "start"),
             "whisper-medium": (12, 4, "start"), "whisper-large-v3": (-14, 0, "end"),
             "whisper-large-v2": None}
GPU_PLACE = {"whisper-tiny": (0, -26, "middle"), "whisper-base": (12, 4, "start"),
             "whisper-small": (12, 4, "start"), "whisper-medium": (-12, -10, "end")}


def load_speed(runs=RUNS):
    newest = {}
    for path in sorted(runs.glob("*-stt-*.json")):  # names start with a UTC stamp
        m = re.match(r"\d{8}T\d{6}Z-stt-(.+)-(arch-ssd-cpu|gtx1650)\.json$", path.name)
        if not m:
            continue
        s = json.loads(path.read_text())["summary"][0]
        if s["ok"] < s["n"] or s["n"] < 50:
            continue  # partial or crashed run
        newest[(m.group(1), "cpu" if m.group(2) == "arch-ssd-cpu" else "gtx1650")] = s
    return newest


def load_memory(runs=RUNS):
    mem = {}
    for path in sorted(runs.glob("*-stt-memory.json")):
        for p in json.loads(path.read_text())["pods"]:
            mem[(p["model"], p["hw"])] = p
    return mem


def spread(s):
    """The middle 90% of requests (p5-p95), when the run recorded p5."""
    if s.get("stt_ms_p5") is None:
        return {}
    return {"ylo": s["stt_ms_p5"] / 1000, "yhi": s["stt_ms_p95"] / 1000}


def cpu_chart(theme, speed, mem):
    pts = []
    for (model, hw), s in sorted(speed.items(), key=lambda kv: kv[1]["stt_ms_p50"]):
        if hw != "cpu" or (model, hw) not in mem:
            continue
        sec, ram = s["stt_ms_p50"] / 1000, mem[(model, hw)]["ram_peak_mib"]
        label = NAMES[model] + (" (live)" if (model, hw) == LIVE else "")
        value = f"{sec:.2f} s · {mib(ram)}"
        if model == "whisper-large-v3":
            v2 = speed[("whisper-large-v2", "cpu")]["stt_ms_p50"] / 1000
            label, value = "Whisper large-v2 / v3", f"{v2:.1f} s / {sec:.1f} s · {mib(ram)}"
        pts.append({"x": ram, "y": sec, "kind": "cpu", "label": NAMES[model], "value": value,
                    **spread(s),
                    "place": CPU_PLACE.get(model, (12, 4, "start"))} | ({"label": label} if label else {}))
    return scatter(theme, title="STT on the home server's CPU: seconds per request by RAM",
                   legend=[("cpu", "Intel i5-10300H, 8 threads")], points=pts,
                   x=(100, 6000, True), y=(0.2, 40, True),
                   xticks=[(100, "100 MiB"), (200, "200 MiB"), (500, "500 MiB"),
                           (1024, "1 GB"), (2048, "2 GB"), (5120, "5 GB")],
                   yticks=[(v, f"{v:g} s") for v in (0.2, 0.5, 1, 2, 5, 10, 20)],
                   xlabel="peak RAM the STT pod held (log scale)",
                   note="dot: median · bar: 5th-95th percentile over 5 sentences × 10 reps · log axes")


def gpu_chart(theme, speed, mem):
    pts = []
    for (model, hw), s in sorted(speed.items(), key=lambda kv: kv[1]["stt_ms_p50"]):
        if hw != "gtx1650" or "vram_peak_mib" not in mem.get((model, hw), {}):
            continue
        sec, vram = s["stt_ms_p50"] / 1000, mem[(model, hw)]["vram_peak_mib"]
        pts.append({"x": vram, "y": sec, "kind": "gtx1650", "label": NAMES[model], **spread(s),
                    "value": f"{sec:.2f} s · {mib(vram)}", "place": GPU_PLACE.get(model, (12, 4, "start"))})
    return scatter(theme, title="STT on the GTX 1650: seconds per request by VRAM",
                   legend=[("gtx1650", "GTX 1650, 4 GB")], points=pts,
                   x=(100, 6000, True), y=(0, 12, False),
                   xticks=[(100, "100 MiB"), (200, "200 MiB"), (500, "500 MiB"),
                           (1024, "1 GB"), (2048, "2 GB"), (4096, "4 GB")],
                   yticks=[(v, f"{v} s") for v in (0, 2, 4, 6, 8, 10, 12)],
                   xlabel="VRAM in use on the card during the run (log scale)",
                   note="dot: median · bar: 5th-95th percentile over 5 sentences × 10 reps",
                   limits=[(GTX1650_MIB, "card limit · Whisper large-v2 / v3 ran out of memory")])


def main():
    speed, mem = load_speed(), load_memory()
    root = ROOT / "assets" / "charts"
    root.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        for stem, draw in (("stt-cpu-speed-vs-ram", cpu_chart), ("stt-gpu-speed-vs-vram", gpu_chart)):
            path = root / f"{stem}-{theme}.svg"
            path.write_text(draw(theme, speed, mem))
            print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
