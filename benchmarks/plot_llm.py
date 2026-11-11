#!/usr/bin/env python3
"""Draw the README's LLM chart, light and dark, standard library only.

    python benchmarks/plot_llm.py

writes assets/charts/llm-reply-vs-vram-{light,dark}.svg: each model's median
whole-reply time (thinking off) against the VRAM its llama.cpp server held,
one marker shape per card, with each card's limit marked. Labels also give
time to first token and generation speed.

Points come from benchmarks/runs/llm/*.json (benchmarks/llm/bench_llm.py),
newest run per model label. VRAM is the whole card as nvidia-smi reports it
after load, so on the RTX 4060 it includes the 40K-token KV cache the live
server is configured with; the GTX 1650 runs a 4K context.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chart_svg import THEMES, mib, scatter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "benchmarks" / "runs" / "llm"

NAMES = {"qwen3-8b": "Qwen3 8B", "granite-4.2-8b": "Granite 4.2 8B", "llama-3.1-8b": "Llama 3.1 8B",
         "qwen3.5-4b": "Qwen3.5 4B", "qwen3.5-2b": "Qwen3.5 2B", "granite-4.2-3b": "Granite 4.2 3B",
         "ministral-3-3b": "Ministral 3 3B", "phi-4-mini": "Phi-4 mini", "gemma-4-e4b": "Gemma 4 E4B"}
LIVE = {"qwen3-8b-rtx4060": "live", "qwen3.5-4b-gtx1650": "standby"}
PLACE = {"qwen3-8b-rtx4060": (-12, 4, "end"), "llama-3.1-8b-rtx4060": (-12, 4, "end"),
         "granite-4.2-8b-rtx4060": (-12, 4, "end"), "qwen3.5-2b-gtx1650": (-12, 8, "end"),
         "granite-4.2-3b-gtx1650": (12, 4, "start"), "qwen3.5-4b-gtx1650": (-12, 4, "end"),
         "phi-4-mini-gtx1650": (0, 30, "middle"), "ministral-3-3b-gtx1650": (12, 4, "start")}


def pct(ordered, p):
    k = (len(ordered) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def load(runs=RUNS):
    newest = {}
    for path in sorted(runs.glob("*.json")):  # names start with a stamp
        run = json.loads(path.read_text())
        newest[run["model"]["label"]] = run
    return newest


def chart(theme, runs):
    pts, failed = [], []
    for label, run in sorted(runs.items()):
        model, card = label.rsplit("-", 1)
        name = NAMES.get(model, model) + (f" ({LIVE[label]})" if label in LIVE else "")
        if run.get("error") or "think_off" not in run:
            failed.append(name)
            continue
        s = run["think_off"]["summary"]
        totals = sorted(r["total_s"] for r in run["think_off"]["rows"] if r.get("total_s") is not None)
        pts.append({"x": run["vram_loaded_mib"], "y": s["total_s_p50"], "kind": card, "label": name,
                    "ylo": pct(totals, 5), "yhi": pct(totals, 95),
                    "value": f"{s['total_s_p50']:.2f} s · {s['gen_tps_p50']:.0f} tok/s",
                    "place": PLACE.get(label, (12, 4, "start"))})
    note = "dot: median · bar: 5th-95th percentile of 50 replies to 5 sentences · log scale"
    limits = [(4096, "GTX 1650 limit" + (f" · {', '.join(failed)} didn't load" if failed else "")),
              (8192, "RTX 4060 limit")]
    return scatter(theme, title="LLMs: median seconds per reply by VRAM",
                   legend=[("gtx1650", "GTX 1650, 4 GB"), ("rtx4060", "RTX 4060, 8 GB")], points=pts,
                   x=(0, 8800, False), y=(0.2, 15, True),
                   xticks=[(v, mib(v) if v else "0") for v in (0, 2048, 4096, 6144, 8192)],
                   yticks=[(v, f"{v:g} s") for v in (0.2, 0.5, 1, 2, 5, 10)],
                   xlabel="VRAM in use on the card with the model loaded",
                   note=note, limits=limits, width=860, height=520)


def main():
    runs = load()
    root = ROOT / "assets" / "charts"
    for theme in THEMES:
        path = root / f"llm-reply-vs-vram-{theme}.svg"
        path.write_text(chart(theme, runs))
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
