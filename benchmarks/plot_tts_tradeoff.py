#!/usr/bin/env python3
"""Draw the README's TTS speed-vs-memory chart as two static SVGs (light and
dark), standard library only.

    python benchmarks/plot_tts_tradeoff.py

writes assets/charts/tts-speed-vs-memory-{light,dark}.svg. The README shows
whichever matches the viewer's theme via <picture>.

The points come from the committed run summaries in benchmarks/runs/tts_engines/
(written by benchmarks/tts_engines/bench_tts_engines.py --keep): for each
engine label, the newest run that measured it. Nothing here is typed in by
hand; to update the chart, re-run the benchmark with --keep and then this.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "benchmarks" / "runs" / "tts_engines"

# Labels measured but not drawn: plain "Kitten nano" is the same engine as the
# live 1.6x point, and would sit on top of it.
HIDE = {"Kitten nano"}
# Where each label goes relative to its mark, so labels don't collide.
PLACE = {"Kitten nano 1.6x (live)": "below", "Chatterbox Nano": "above",
         "Chatterbox Turbo": "left"}


def load_points(runs=RUNS):
    """(label, memory MiB, x realtime, device, placement, n) per engine label,
    taken from the newest kept run that measured it."""
    newest = {}
    for path in sorted(runs.glob("*.json")):  # names start with a UTC stamp
        run = json.loads(path.read_text())
        device = run["metadata"].get("device", "cpu")
        for entry in run.get("summary", []):
            if entry.get("x_realtime") and entry.get("rss_peak_mib"):
                newest[entry["label"]] = (entry, device)
    return [(label, entry["rss_peak_mib"], entry["x_realtime"], device,
             PLACE.get(label, "right"), entry.get("n"))
            for label, (entry, device) in sorted(newest.items()) if label not in HIDE]


THEMES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e",
              "grid": "#e4e3df", "axis": "#b9b8b2", "cpu": "#2a78d6", "gpu": "#eb6834"},
    "dark": {"surface": "#1a1a19", "text": "#ffffff", "muted": "#c3c2b7",
             "grid": "#2e2e2c", "axis": "#55544f", "cpu": "#3987e5", "gpu": "#d95926"},
}
SERIES = {"cpu": "CPU on the home server (process RAM)",
          "gpu": "GPU, RTX 4060 (VRAM)"}

W, H = 780, 470
LEFT, RIGHT, TOP, BOTTOM = 76, 730, 100, 398
X_MAX, Y_MAX = 3000, 12


def sx(mib):
    return LEFT + (RIGHT - LEFT) * mib / X_MAX


def sy(seconds):
    return BOTTOM - (BOTTOM - TOP) * seconds / Y_MAX


def marker(kind, x, y, colour, surface):
    # 2px surface ring so overlapping marks stay separable; shape differs by
    # series so identity never rests on colour alone.
    if kind == "cpu":
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{colour}" '
                f'stroke="{surface}" stroke-width="2"/>')
    return (f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" rx="2" '
            f'fill="{colour}" stroke="{surface}" stroke-width="2"/>')


def svg(theme, points):
    t = THEMES[theme]
    font = 'font-family="system-ui, -apple-system, Segoe UI, sans-serif"'
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
           f'height="{H}" role="img" aria-labelledby="t d" {font}>',
           '<title id="t">TTS engines: time to make 10 s of speech vs memory</title>',
           '<desc id="d">' + "; ".join(
               f"{label}: {10 / rt:.1f} s, {mib} MiB, {kind.upper()}"
               for label, mib, rt, kind, _, _ in points) + "</desc>",
           f'<rect width="{W}" height="{H}" fill="{t["surface"]}"/>',
           f'<text x="{LEFT}" y="30" font-size="16" font-weight="600" fill="{t["text"]}">'
           'Seconds to make 10 s of speech, by memory used</text>']

    # Legend, one row above the plot.
    lx = LEFT
    for kind, name in SERIES.items():
        out.append(marker(kind, lx + 6, 54, t[kind], t["surface"]))
        out.append(f'<text x="{lx + 18}" y="58" font-size="12.5" fill="{t["muted"]}">{name}</text>')
        lx += 20 + 7.1 * len(name) + 28

    # Recessive grid and axes.
    for s in range(0, Y_MAX + 1, 3):
        y = sy(s)
        out.append(f'<line x1="{LEFT}" x2="{RIGHT}" y1="{y:.1f}" y2="{y:.1f}" '
                   f'stroke="{t["grid"] if s else t["axis"]}" stroke-width="1"/>')
        out.append(f'<text x="{LEFT - 10}" y="{y + 4:.1f}" font-size="12" text-anchor="end" '
                   f'fill="{t["muted"]}">{s} s</text>')
    for m in range(0, X_MAX + 1, 500):
        x = sx(m)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{BOTTOM}" y2="{BOTTOM + 5}" stroke="{t["axis"]}"/>')
        tick = "0" if m == 0 else f"{m:,} MiB"
        out.append(f'<text x="{x:.1f}" y="{BOTTOM + 20}" font-size="12" text-anchor="middle" '
                   f'fill="{t["muted"]}">{tick}</text>')
    out.append(f'<text x="{(LEFT + RIGHT) / 2}" y="{BOTTOM + 44}" font-size="12.5" '
               f'text-anchor="middle" fill="{t["muted"]}">memory the engine holds</text>')
    out.append(f'<text x="{RIGHT}" y="{TOP - 8}" font-size="11.5" text-anchor="end" '
               f'fill="{t["muted"]}">lower is faster, further left is lighter</text>')

    # Marks and direct labels (six points: every one is labelled).
    for label, mib, rt, kind, where, n in points:
        x, y = sx(mib), sy(10 / rt)
        out.append(marker(kind, x, y, t[kind], t["surface"]))
        value = f"{10 / rt:.1f} s · {mib:.0f} MiB" + (f" · n={n}" if n else "")
        if where == "right":
            ax, ay, anchor = x + 12, y - 2, "start"
        elif where == "left":
            ax, ay, anchor = x - 12, y - 2, "end"
        elif where == "above":
            ax, ay, anchor = x, y - 26, "middle"
        else:  # below
            ax, ay, anchor = x + 4, y + 22, "start"
        out.append(f'<text x="{ax:.1f}" y="{ay:.1f}" font-size="12.5" font-weight="600" '
                   f'text-anchor="{anchor}" fill="{t["text"]}">{label}</text>')
        out.append(f'<text x="{ax:.1f}" y="{ay + 15:.1f}" font-size="11.5" '
                   f'text-anchor="{anchor}" fill="{t["muted"]}">{value}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    points = load_points()
    root = ROOT / "assets" / "charts"
    root.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        path = root / f"tts-speed-vs-memory-{theme}.svg"
        path.write_text(svg(theme, points))
        print(path.relative_to(root.parents[1]))


if __name__ == "__main__":
    main()
